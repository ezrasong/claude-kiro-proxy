//! Kiro owns authentication and translation in its gateway. This provider routes
//! native Anthropic traffic to a selected local account and normalizes API quirks.
use std::{collections::BTreeMap, path::PathBuf, time::Duration};

use anyhow::{Result, bail};
use async_trait::async_trait;
use axum::{
    Json,
    body::Body,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use futures_util::StreamExt;
use serde::Deserialize;

use crate::{
    anthropic::{json_error, schema::MessagesRequest},
    provider::{
        CliHandlers, Generation, GenerationBody, Provider, ProviderError, ProviderErrorKind,
        RequestContext,
    },
};

// The gateway waits for Kiro before sending headers. Buffered responses wait
// for the entire generation. Allow the gateway's 300s upstream inactivity
// deadline to return its error before the local connection deadline expires.
const GATEWAY_TIMEOUT: Duration = Duration::from_secs(330);

#[derive(Deserialize)]
struct Accounts {
    active: Option<String>,
    accounts: BTreeMap<String, Account>,
}

#[derive(Clone, Deserialize)]
struct Account {
    #[serde(rename = "baseUrl")]
    base_url: String,
    #[serde(rename = "apiKey")]
    api_key: String,
}

pub struct KiroProvider {
    accounts_file: PathBuf,
    client: reqwest::Client,
}

impl Default for KiroProvider {
    fn default() -> Self {
        Self::new()
    }
}

impl KiroProvider {
    pub fn new() -> Self {
        let path = std::env::var_os("CCP_KIRO_ACCOUNTS_FILE")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                crate::config::load_config()
                    .config_dir
                    .join("kiro-accounts.json")
            });
        Self::with_accounts_file(path)
    }

    pub fn with_accounts_file(accounts_file: PathBuf) -> Self {
        Self {
            accounts_file,
            client: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(10))
                .read_timeout(GATEWAY_TIMEOUT)
                .build()
                .expect("local Kiro HTTP client"),
        }
    }

    async fn active_account(&self) -> Result<Account, ProviderError> {
        let raw = tokio::fs::read(&self.accounts_file)
            .await
            .map_err(|_| configuration_error())?;
        let accounts: Accounts = serde_json::from_slice(&raw).map_err(|_| configuration_error())?;
        let account = accounts
            .active
            .as_ref()
            .and_then(|name| accounts.accounts.get(name))
            .cloned()
            .ok_or_else(configuration_error)?;
        let url = reqwest::Url::parse(&account.base_url).map_err(|_| configuration_error())?;
        if url.scheme() != "http"
            || !matches!(url.host_str(), Some("127.0.0.1" | "[::1]"))
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || url.path() != "/"
            || url.port().is_none()
            || account.api_key.is_empty()
            || http::HeaderValue::from_str(&account.api_key).is_err()
        {
            return Err(configuration_error());
        }
        Ok(account)
    }

    async fn post(
        &self,
        mut body: MessagesRequest,
        ctx: &RequestContext,
        endpoint: &str,
    ) -> Result<reqwest::Response, ProviderError> {
        let model = resolve_model(body.model.as_deref().unwrap_or_default())?;
        body.model = Some(model.clone());
        normalize_system_messages(&mut body)?;
        // Read once per request: a manual swap never changes an in-flight stream.
        let account = self.active_account().await?;
        if let Some(monitor) = &ctx.monitor {
            monitor.model_resolved(&ctx.req_id, model);
            monitor.upstream_started(&ctx.req_id);
        }
        if let Some(traffic) = &ctx.traffic {
            traffic.write_json(
                "020-upstream-request",
                &serde_json::to_value(&body).unwrap_or_default(),
            );
        }
        let response = tokio::time::timeout(
            GATEWAY_TIMEOUT,
            self.client
                .post(format!(
                    "{}{endpoint}",
                    account.base_url.trim_end_matches('/')
                ))
                .header("x-api-key", account.api_key)
                .header("anthropic-version", "2023-06-01")
                .header(
                    "accept",
                    if body.stream {
                        "text/event-stream"
                    } else {
                        "application/json"
                    },
                )
                .json(&body)
                .send(),
        )
        .await
        .map_err(|_| transport_error())?
        .map_err(|_| transport_error())?;
        if response.status().is_success() {
            return Ok(response);
        }
        let status = response.status();
        let kind = match status.as_u16() {
            401 => ProviderErrorKind::Authentication,
            402 | 403 => ProviderErrorKind::Permission,
            429 => ProviderErrorKind::RateLimit,
            400 | 404 | 413 | 422 => ProviderErrorKind::InvalidRequest,
            _ => ProviderErrorKind::Api,
        };
        // Never expose gateway error bodies: they may contain account credentials.
        let mut error = ProviderError::new(
            status,
            kind,
            format!(
                "Selected Kiro account returned HTTP {status}. Check its gateway log or select another account."
            ),
        );
        error.retry_after = response
            .headers()
            .get("retry-after")
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned);
        Err(error)
    }

    async fn buffered(
        &self,
        body: MessagesRequest,
        ctx: RequestContext,
        endpoint: &str,
    ) -> Response {
        let result = async {
            let mut stream = self.post(body, &ctx, endpoint).await?.bytes_stream();
            let mut bytes = Vec::new();
            while let Some(chunk) = stream.next().await {
                let chunk = chunk.map_err(|_| transport_error())?;
                if bytes.len().saturating_add(chunk.len()) > 8 * 1024 * 1024 {
                    return Err(transport_error());
                }
                bytes.extend_from_slice(&chunk);
            }
            let value: serde_json::Value =
                serde_json::from_slice(&bytes).map_err(|_| transport_error())?;
            if let Some(monitor) = &ctx.monitor {
                let input = value
                    .pointer("/usage/input_tokens")
                    .or_else(|| value.get("input_tokens"))
                    .and_then(|v| v.as_u64());
                let output = value
                    .pointer("/usage/output_tokens")
                    .and_then(|v| v.as_u64());
                monitor.usage_updated(&ctx.req_id, input, output);
            }
            if let Some(traffic) = &ctx.traffic {
                traffic.write_json("051-downstream-response", &value);
            }
            Ok((StatusCode::OK, Json(value)).into_response())
        }
        .await;
        result.unwrap_or_else(error_response)
    }
}

fn normalize_system_messages(body: &mut MessagesRequest) -> Result<(), ProviderError> {
    if !body.messages.iter().any(|message| message.role == "system") {
        return Ok(());
    }
    // Claude/Caveman may embed system instructions in the message history.
    // Anthropic's native schema puts these in the top-level system field.
    let mut blocks = Vec::new();
    if let Some(system) = body.extra.remove("system") {
        append_system_blocks(&mut blocks, system)?;
    }
    let mut messages = Vec::new();
    for message in std::mem::take(&mut body.messages) {
        if message.role == "system" {
            append_system_blocks(&mut blocks, message.content)?;
        } else {
            messages.push(message);
        }
    }
    body.messages = messages;
    body.extra
        .insert("system".into(), serde_json::Value::Array(blocks));
    Ok(())
}

fn append_system_blocks(
    blocks: &mut Vec<serde_json::Value>,
    content: serde_json::Value,
) -> Result<(), ProviderError> {
    match content {
        serde_json::Value::String(text) => {
            blocks.push(serde_json::json!({"type": "text", "text": text}));
        }
        serde_json::Value::Array(items)
            if items.iter().all(|item| {
                item.get("type").and_then(|value| value.as_str()) == Some("text")
                    && item.get("text").is_some_and(|value| value.is_string())
            }) =>
        {
            blocks.extend(items);
        }
        _ => {
            return Err(ProviderError::new(
                StatusCode::BAD_REQUEST,
                ProviderErrorKind::InvalidRequest,
                "Kiro system instructions must contain text or text blocks.",
            ));
        }
    }
    Ok(())
}

pub fn advertised_models() -> Vec<String> {
    ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5"]
        .into_iter()
        .map(|m| format!("kiro:{m}"))
        .collect()
}

fn resolve_model(raw: &str) -> Result<String, ProviderError> {
    let model = raw.strip_prefix("kiro:").unwrap_or(raw);
    if model.is_empty()
        || model.len() > 200
        || !model
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
    {
        return Err(ProviderError::new(
            StatusCode::BAD_REQUEST,
            ProviderErrorKind::InvalidRequest,
            "Use kiro:<model-id> with a model available to your Kiro account.",
        ));
    }
    Ok(match model {
        "sonnet" => "claude-sonnet-4-5",
        "haiku" => "claude-haiku-4-5",
        "opus" => "claude-opus-4-5",
        _ => model,
    }
    .to_owned())
}

fn configuration_error() -> ProviderError {
    ProviderError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        ProviderErrorKind::Authentication,
        "No valid local Kiro account selected. Run scripts/stack.py account add, then account use and serve.",
    )
}

fn transport_error() -> ProviderError {
    ProviderError::new(
        StatusCode::BAD_GATEWAY,
        ProviderErrorKind::Api,
        "Kiro gateway is unavailable or returned an invalid response. Run scripts/stack.py serve and check the selected account.",
    )
}

fn error_response(error: ProviderError) -> Response {
    let mut response = json_error(error.status, error.error_type(), &error.message);
    if let Some(value) = error.retry_after.and_then(|v| v.parse().ok()) {
        response.headers_mut().insert("retry-after", value);
    }
    response
}

#[async_trait]
impl Provider for KiroProvider {
    fn name(&self) -> &'static str {
        "kiro"
    }
    fn supported_models(&self) -> Vec<String> {
        advertised_models()
    }
    fn cli(&self) -> &'static dyn CliHandlers {
        &KIRO_CLI
    }

    async fn handle_messages(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        if !body.stream {
            return self.buffered(body, ctx, "/v1/messages").await;
        }
        match self.generate_anthropic_stream(body, ctx).await {
            Ok(generation) => Response::builder()
                .header("content-type", "text/event-stream")
                .header("cache-control", "no-cache")
                .body(match generation.body {
                    GenerationBody::LiveSse(body) => body,
                    GenerationBody::BufferedSse(bytes) => Body::from(bytes),
                })
                .expect("SSE response"),
            Err(error) => error_response(error),
        }
    }

    async fn handle_count_tokens(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        self.buffered(body, ctx, "/v1/messages/count_tokens").await
    }

    async fn generate_anthropic_stream(
        &self,
        mut body: MessagesRequest,
        ctx: RequestContext,
    ) -> Result<Generation, ProviderError> {
        body.stream = true;
        let model = resolve_model(body.model.as_deref().unwrap_or_default())?;
        let response = self.post(body, &ctx, "/v1/messages").await?;
        if !response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.starts_with("text/event-stream"))
        {
            return Err(transport_error());
        }
        Ok(Generation {
            body: GenerationBody::LiveSse(crate::anthropic::passthrough::stream_body(
                response.bytes_stream(),
                "Kiro",
                ctx.monitor,
                ctx.req_id,
                ctx.traffic,
            )),
            resolved_model: model,
        })
    }
}

struct KiroCli;
static KIRO_CLI: KiroCli = KiroCli;
impl CliHandlers for KiroCli {
    fn login(&self) -> Result<()> {
        bail!("Sign in through Kiro, then import with scripts/stack.py account add.")
    }
    fn device(&self) -> Result<()> {
        self.login()
    }
    fn status(&self) -> Result<()> {
        self.login()
    }
    fn logout(&self) -> Result<()> {
        bail!("Kiro accounts are managed with scripts/stack.py account commands.")
    }
}
