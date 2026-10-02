use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
};
use claude_code_proxy::{
    config::{AliasProvider, load_config_for_env},
    provider::{Provider, RequestContext},
    providers::kiro::KiroProvider,
    registry::Registry,
};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tempfile::TempDir;
use tokio::sync::Notify;

#[derive(Clone)]
struct Gateway {
    name: &'static str,
    requests: Arc<Mutex<Vec<(HeaderMap, Value)>>>,
    release: Arc<Notify>,
    status: StatusCode,
}

async fn messages(
    State(gateway): State<Gateway>,
    headers: HeaderMap,
    Json(value): Json<Value>,
) -> Response {
    gateway
        .requests
        .lock()
        .unwrap()
        .push((headers, value.clone()));
    if gateway.status != StatusCode::OK {
        return (
            gateway.status,
            [("retry-after", "7")],
            Json(json!({"error": "secret-token-in-error"})),
        )
            .into_response();
    }
    if value["stream"] == true {
        let name = gateway.name;
        let release = gateway.release.clone();
        let stream = futures_util::stream::unfold(0, move |phase| {
            let release = release.clone();
            async move {
                match phase {
                    0 => Some((
                        Ok::<_, std::io::Error>(bytes::Bytes::from(
                            "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5}}}\n\n",
                        )),
                        1,
                    )),
                    1 => {
                        release.notified().await;
                        Some((
                            Ok(bytes::Bytes::from(format!(
                                "event: content_block_delta\ndata: {{\"type\":\"content_block_delta\",\"delta\":{{\"type\":\"text_delta\",\"text\":\"{name}\"}}}}\n\nevent: message_stop\ndata: {{\"type\":\"message_stop\"}}\n\n"
                            ))),
                            2,
                        ))
                    }
                    _ => None,
                }
            }
        });
        return Response::builder()
            .header("content-type", "text/event-stream")
            .body(Body::from_stream(stream))
            .unwrap();
    }
    Json(json!({"type": "message", "model": value["model"], "content": [{"type":"text", "text": gateway.name}], "usage": {"input_tokens":5,"output_tokens":2}})).into_response()
}

async fn count(
    State(gateway): State<Gateway>,
    headers: HeaderMap,
    Json(value): Json<Value>,
) -> Json<Value> {
    gateway.requests.lock().unwrap().push((headers, value));
    Json(json!({"input_tokens": 123}))
}

async fn gateway(
    name: &'static str,
    status: StatusCode,
) -> (String, Gateway, tokio::task::JoinHandle<()>) {
    let state = Gateway {
        name,
        status,
        requests: Arc::default(),
        release: Arc::default(),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let router = Router::new()
        .route("/v1/messages", post(messages))
        .route("/v1/messages/count_tokens", post(count))
        .with_state(state.clone());
    let task = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    (url, state, task)
}

fn ctx() -> RequestContext {
    RequestContext {
        req_id: "kiro-test".into(),
        session_id: None,
        session_seq: None,
        provider: "kiro".into(),
        traffic: None,
        monitor: None,
    }
}

fn request(stream: bool) -> claude_code_proxy::anthropic::MessagesRequest {
    serde_json::from_value(json!({"model":"kiro:claude-sonnet-4-5", "stream":stream, "max_tokens":321,
        "messages":[{"role":"user", "content":[{"type":"image", "source":{"type":"base64", "media_type":"image/png", "data":"aGVsbG8="}}, {"type":"text", "text":"hello"}]}],
        "system":[{"type":"text", "text":"system prompt"}],
        "tools":[{"name":"read", "input_schema":{"type":"object"}}], "thinking":{"type":"enabled", "budget_tokens":128}
    })).unwrap()
}

async fn value(response: Response) -> Value {
    serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes()).unwrap()
}

#[tokio::test]
async fn account_swap_preserves_active_stream_and_native_payload_and_count_tokens() {
    let (url_a, a, task_a) = gateway("account-a", StatusCode::OK).await;
    let (url_b, b, task_b) = gateway("account-b", StatusCode::OK).await;
    let dir = TempDir::new().unwrap();
    let file = dir.path().join("accounts.json");
    let mut accounts = json!({"active":"a", "accounts":{"a":{"baseUrl":url_a, "apiKey":"key-a"}, "b":{"baseUrl":url_b, "apiKey":"key-b"}}});
    std::fs::write(&file, accounts.to_string()).unwrap();
    let provider = KiroProvider::with_accounts_file(file.clone());
    let response = provider.handle_messages(request(true), ctx()).await;
    assert_eq!(response.status(), StatusCode::OK);
    let first =
        tokio::spawn(async move { response.into_body().collect().await.unwrap().to_bytes() });
    accounts["active"] = json!("b");
    std::fs::write(&file, accounts.to_string()).unwrap();
    let second = value(provider.handle_messages(request(false), ctx()).await).await;
    assert_eq!(second["content"][0]["text"], "account-b");
    a.release.notify_one();
    assert!(String::from_utf8_lossy(&first.await.unwrap()).contains("account-a"));
    let counted = value(provider.handle_count_tokens(request(false), ctx()).await).await;
    assert_eq!(counted["input_tokens"], 123);
    let captured = b.requests.lock().unwrap();
    let (headers, payload) = &captured[0];
    assert_eq!(headers["x-api-key"], "key-b");
    assert_eq!(headers["anthropic-version"], "2023-06-01");
    assert!(!headers.contains_key("authorization"));
    assert_eq!(payload["model"], "claude-sonnet-4-5");
    let original = serde_json::to_value(request(false)).unwrap();
    for key in ["system", "messages", "tools", "thinking", "max_tokens"] {
        assert_eq!(payload[key], original[key]);
    }
    task_a.abort();
    task_b.abort();
}

#[tokio::test]
async fn embedded_system_messages_use_native_field_for_generation_and_token_counting() {
    let (url, gateway, task) = gateway("system-test", StatusCode::OK).await;
    let dir = TempDir::new().unwrap();
    let file = dir.path().join("accounts.json");
    std::fs::write(
        &file,
        json!({"active":"a","accounts":{"a":{"baseUrl":url,"apiKey":"a"}}}).to_string(),
    )
    .unwrap();
    let provider = KiroProvider::with_accounts_file(file);
    let mut body = request(false);
    body.messages.insert(
        1,
        serde_json::from_value(json!({"role":"system","content":"continued instruction"})).unwrap(),
    );
    body.messages.push(serde_json::from_value(json!({"role":"system","content":[{"type":"text","text":"cached instruction","cache_control":{"type":"ephemeral"}}]})).unwrap());
    let original_messages = request(false).messages;
    assert_eq!(
        provider.handle_messages(body.clone(), ctx()).await.status(),
        StatusCode::OK
    );
    assert_eq!(
        provider
            .handle_count_tokens(body.clone(), ctx())
            .await
            .status(),
        StatusCode::OK
    );
    {
        let captured = gateway.requests.lock().unwrap();
        for (_, payload) in captured.iter() {
            assert_eq!(
                payload["messages"],
                serde_json::to_value(&original_messages).unwrap()
            );
            assert_eq!(
                payload["system"],
                json!([
                    {"type":"text","text":"system prompt"},
                    {"type":"text","text":"continued instruction"},
                    {"type":"text","text":"cached instruction","cache_control":{"type":"ephemeral"}}
                ])
            );
        }
    }
    body.extra.insert("system".into(), json!("string system"));
    assert_eq!(
        provider.handle_messages(body.clone(), ctx()).await.status(),
        StatusCode::OK
    );
    assert_eq!(
        gateway.requests.lock().unwrap()[2].1["system"][0]["text"],
        "string system"
    );
    body.messages[1].content = json!([{"type":"image","source":{}}]);
    assert_eq!(
        provider.handle_messages(body, ctx()).await.status(),
        StatusCode::BAD_REQUEST
    );
    assert_eq!(gateway.requests.lock().unwrap().len(), 3);
    task.abort();
}

#[tokio::test]
async fn tool_results_are_preserved_in_streaming_buffered_and_count_requests() {
    let (url, gateway, task) = gateway("continued-answer", StatusCode::OK).await;
    let dir = TempDir::new().unwrap();
    let file = dir.path().join("accounts.json");
    std::fs::write(
        &file,
        json!({"active":"a","accounts":{"a":{"baseUrl":url,"apiKey":"a"}}}).to_string(),
    )
    .unwrap();
    let provider = KiroProvider::with_accounts_file(file);
    let mut body = request(false);
    let history = json!([
        {"role":"user","content":"Check both values and report the result."},
        {"role":"assistant","content":[
            {"type":"tool_use","id":"toolu_a","name":"read","input":{"path":"a"}},
            {"type":"tool_use","id":"toolu_b","name":"read","input":{"path":"b"}}
        ]},
        {"role":"user","content":[
            {"type":"tool_result","tool_use_id":"toolu_a","content":[{"type":"text","text":"value a"}],"cache_control":{"type":"ephemeral"}},
            {"type":"tool_result","tool_use_id":"toolu_b","content":"not found","is_error":true}
        ]}
    ]);
    body.messages = serde_json::from_value(history.clone()).unwrap();
    assert_eq!(
        value(provider.handle_messages(body.clone(), ctx()).await).await["content"][0]["text"],
        "continued-answer"
    );
    body.stream = true;
    gateway.release.notify_one();
    let response = provider.handle_messages(body.clone(), ctx()).await;
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    assert!(String::from_utf8_lossy(&bytes).contains("continued-answer"));
    assert_eq!(
        value(provider.handle_count_tokens(body, ctx()).await).await["input_tokens"],
        123
    );
    let captured = gateway.requests.lock().unwrap();
    assert_eq!(captured.len(), 3);
    for (_, payload) in captured.iter() {
        assert_eq!(payload["messages"][0], history[0]);
        assert_eq!(payload["messages"][1], history[1]);
        let blocks = payload["messages"][2]["content"].as_array().unwrap();
        assert_eq!(blocks, history[2]["content"].as_array().unwrap());
    }
    task.abort();
}

#[tokio::test]
async fn explicit_user_text_and_other_messages_are_preserved() {
    let (url, gateway, task) = gateway("unchanged", StatusCode::OK).await;
    let dir = TempDir::new().unwrap();
    let file = dir.path().join("accounts.json");
    std::fs::write(
        &file,
        json!({"active":"a","accounts":{"a":{"baseUrl":url,"apiKey":"a"}}}).to_string(),
    )
    .unwrap();
    let provider = KiroProvider::with_accounts_file(file);
    for message in [
        json!({"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_a","content":"ok"},{"type":"text","text":"Stop here and summarize.","cache_control":{"type":"ephemeral"}}]}),
        json!({"role":"user","content":"User instruction"}),
        json!({"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"aGVsbG8="}}]}),
        json!({"role":"assistant","content":[{"type":"tool_use","id":"toolu_a","name":"read","input":{}}]}),
    ] {
        let mut body = request(false);
        body.messages = serde_json::from_value(json!([message.clone()])).unwrap();
        provider.handle_messages(body, ctx()).await;
        assert_eq!(
            gateway.requests.lock().unwrap().last().unwrap().1["messages"],
            json!([message])
        );
    }
    task.abort();
}

#[tokio::test]
async fn manual_selection_never_falls_back_on_quota_and_redacts_error_bodies() {
    let (url_a, _, task_a) = gateway("limited", StatusCode::TOO_MANY_REQUESTS).await;
    let (url_b, b, task_b) = gateway("other", StatusCode::OK).await;
    let dir = TempDir::new().unwrap();
    let file = dir.path().join("accounts.json");
    std::fs::write(&file, json!({"active":"a", "accounts":{"a":{"baseUrl":url_a,"apiKey":"a"},"b":{"baseUrl":url_b,"apiKey":"b"}}}).to_string()).unwrap();
    let response = KiroProvider::with_accounts_file(file)
        .handle_messages(request(false), ctx())
        .await;
    assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(response.headers()["retry-after"], "7");
    assert!(!value(response).await.to_string().contains("secret-token"));
    assert!(b.requests.lock().unwrap().is_empty());
    task_a.abort();
    task_b.abort();
}

#[tokio::test]
async fn rejects_remote_gateway_and_malformed_selection_before_sending_credentials() {
    let dir = TempDir::new().unwrap();
    let file = dir.path().join("accounts.json");
    let provider = KiroProvider::with_accounts_file(file.clone());
    for raw in ["{broken".to_string(), json!({"active":"a","accounts":{"a":{"baseUrl":"https://example.com:443","apiKey":"secret"}}}).to_string(), json!({"active":"missing", "accounts":{}}).to_string()] {
        std::fs::write(&file, raw).unwrap();
        assert_eq!(provider.handle_messages(request(false), ctx()).await.status(), StatusCode::SERVICE_UNAVAILABLE);
    }
}

#[test]
fn kiro_routing_coexists_with_codex_and_respects_session_aliases() {
    let registry = Registry::new(AliasProvider::Kiro);
    assert_eq!(
        registry
            .provider_for_model("kiro:new-model", None)
            .unwrap()
            .name(),
        "kiro"
    );
    assert_eq!(
        registry
            .provider_for_model("gpt-6-sol", None)
            .unwrap()
            .name(),
        "codex"
    );
    assert_eq!(
        registry.provider_for_model("sonnet", None).unwrap().name(),
        "kiro"
    );
    assert_eq!(
        registry
            .provider_for_model("sonnet", Some(&AliasProvider::Codex))
            .unwrap()
            .name(),
        "codex"
    );
    let config = load_config_for_env(&std::collections::HashMap::from([(
        "CCP_ALIAS_PROVIDER".into(),
        "kiro".into(),
    )]));
    assert_eq!(config.alias_provider, AliasProvider::Kiro);
    let state = claude_code_proxy::session::record_session_request(
        Some("kiro-session"),
        None,
        "kiro",
        "kiro:claude-sonnet-4-5",
        1,
    )
    .unwrap();
    assert_eq!(state.affinity_provider, Some(AliasProvider::Kiro));
}
