use super::client::OpenCodeResponse;
use crate::anthropic::schema::MessagesRequest;
use crate::monitor::MonitorHandle;
use crate::traffic::TrafficCapture;
use axum::body::Body;
use std::sync::Arc;

const DEFAULT_MAX_TOKENS: u32 = 32_000;

pub fn prepare_request(
    body: &MessagesRequest,
    model: &str,
) -> Result<serde_json::Value, serde_json::Error> {
    let mut translated = serde_json::to_value(body)?;
    translated["model"] = serde_json::Value::String(model.to_string());
    translated["max_tokens"] = serde_json::json!(
        body.max_tokens
            .filter(|value| *value > 0)
            .unwrap_or(DEFAULT_MAX_TOKENS)
    );
    if model == "minimax-m3" && translated.get("thinking").is_none() {
        translated["thinking"] = serde_json::json!({"type": "adaptive"});
    }
    Ok(translated)
}

pub fn stream_body(
    upstream: OpenCodeResponse,
    monitor: Option<MonitorHandle>,
    req_id: String,
    traffic: Option<Arc<TrafficCapture>>,
) -> Body {
    crate::anthropic::passthrough::stream_body(
        upstream.into_stream(),
        "OpenCode Go",
        monitor,
        req_id,
        traffic,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn minimax_m3_defaults_to_adaptive_thinking_without_overriding_the_caller() {
        let body: MessagesRequest = serde_json::from_value(json!({
            "model": "minimax-m3",
            "messages": [{"role": "user", "content": "hello"}]
        }))
        .unwrap();
        let translated = prepare_request(&body, "minimax-m3").unwrap();
        assert_eq!(translated["thinking"], json!({"type": "adaptive"}));
        assert_eq!(translated["max_tokens"], DEFAULT_MAX_TOKENS);

        let body: MessagesRequest = serde_json::from_value(json!({
            "model": "minimax-m3",
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": "hello"}],
            "thinking": {"type": "enabled", "budget_tokens": 2048}
        }))
        .unwrap();
        let translated = prepare_request(&body, "minimax-m3").unwrap();
        assert_eq!(
            translated["thinking"],
            json!({"type": "enabled", "budget_tokens": 2048})
        );
        assert_eq!(translated["max_tokens"], 2048);
    }
}
