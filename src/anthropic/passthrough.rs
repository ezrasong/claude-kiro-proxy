use crate::anthropic::sse::encode_sse_event;
use crate::monitor::MonitorHandle;
use crate::providers::grok::translate::stream::SseDecoder;
use crate::traffic::{StreamTrafficCapture, TrafficCapture};
use axum::body::Body;
use bytes::Bytes;
use futures_util::StreamExt;
use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(15);

pub fn stream_body<S, E>(
    upstream: S,
    provider: &'static str,
    monitor: Option<MonitorHandle>,
    req_id: String,
    traffic: Option<Arc<TrafficCapture>>,
) -> Body
where
    S: futures_util::Stream<Item = Result<Bytes, E>> + Send + Unpin + 'static,
    E: 'static,
{
    let state = MessagesStreamState {
        upstream,
        provider,
        decoder: SseDecoder::default(),
        terminal: false,
        error_sent: false,
        monitor,
        req_id,
        bytes: 0,
        chunks: 0,
        stream_capture: traffic.as_ref().map(|traffic| traffic.stream_capture()),
        traffic,
    };
    let stream = futures_util::stream::unfold(state, |mut state| async move {
        state
            .next_output()
            .await
            .map(|bytes| (Ok::<Bytes, Infallible>(bytes), state))
    });
    Body::from_stream(stream)
}

struct MessagesStreamState<S> {
    provider: &'static str,
    upstream: S,
    decoder: SseDecoder,
    terminal: bool,
    error_sent: bool,
    monitor: Option<MonitorHandle>,
    req_id: String,
    bytes: u64,
    chunks: u64,
    stream_capture: Option<StreamTrafficCapture>,
    traffic: Option<Arc<TrafficCapture>>,
}

impl<S, E> MessagesStreamState<S>
where
    S: futures_util::Stream<Item = Result<Bytes, E>> + Unpin,
{
    async fn next_output(&mut self) -> Option<Bytes> {
        if self.terminal {
            return None;
        }
        if self.error_sent {
            self.terminal = true;
            return None;
        }

        // Native Messages gateways can be silent while reasoning or building
        // tool arguments. Keep clients alive without changing upstream deadlines.
        // Raw chunks may split an SSE frame: never insert a ping inside one.
        let next = if self.decoder.finish().is_ok() {
            match tokio::time::timeout(HEARTBEAT_INTERVAL, self.upstream.next()).await {
                Ok(next) => next,
                Err(_) => {
                    let value = serde_json::json!({"type": "ping"});
                    if let Some(capture) = self.stream_capture.as_mut() {
                        capture.downstream_event("ping", value.clone());
                    }
                    return Some(Bytes::from(encode_sse_event(
                        Some("ping"),
                        &value.to_string(),
                    )));
                }
            }
        } else {
            self.upstream.next().await
        };
        let chunk = match next {
            Some(Ok(chunk)) => chunk,
            Some(Err(_)) => return Some(self.fail_at("transport", "upstream_stream")),
            None => {
                if self.decoder.finish().is_err() {
                    return Some(self.fail_at("decoder", "incomplete_stream"));
                }
                return Some(self.fail_at("protocol", "missing_message_stop"));
            }
        };
        if self.bytes == 0
            && let Some(monitor) = self.monitor.as_ref()
        {
            monitor.generation_started(&self.req_id);
        }
        self.bytes = self.bytes.saturating_add(chunk.len() as u64);
        self.chunks = self.chunks.saturating_add(1);

        let events = match self.decoder.push(&chunk) {
            Ok(events) => events,
            Err(_) => return Some(self.fail_at("decoder", "malformed_sse")),
        };
        let mut input_tokens = None;
        let mut output_tokens = None;
        let mut terminal = false;
        for event in events {
            if terminal {
                return Some(self.fail_at("protocol", "event_after_message_stop"));
            }
            let value: serde_json::Value = match serde_json::from_str(&event.data) {
                Ok(value) => value,
                Err(_) => return Some(self.fail_at("json", "malformed_event")),
            };
            if let Some(capture) = self.stream_capture.as_mut() {
                capture.upstream_event(event.event.as_deref(), &value);
                capture
                    .downstream_event(event.event.as_deref().unwrap_or("message"), value.clone());
            }
            input_tokens = value
                .pointer("/message/usage/input_tokens")
                .or_else(|| value.pointer("/usage/input_tokens"))
                .and_then(serde_json::Value::as_u64)
                .or(input_tokens);
            output_tokens = value
                .pointer("/message/usage/output_tokens")
                .or_else(|| value.pointer("/usage/output_tokens"))
                .and_then(serde_json::Value::as_u64)
                .or(output_tokens);
            let kind = value.get("type").and_then(serde_json::Value::as_str);
            if event.event.as_deref() == Some("error") || kind == Some("error") {
                return Some(self.fail_at("upstream", "error_event"));
            }
            if event.event.as_deref() == Some("message_stop") || kind == Some("message_stop") {
                terminal = true;
            }
        }
        if let Some(monitor) = self.monitor.as_ref() {
            monitor.stream_progress(
                &self.req_id,
                chunk.len() as u64,
                1,
                input_tokens,
                output_tokens,
            );
        }
        if terminal {
            if self.decoder.finish().is_err() {
                return Some(self.fail_at("decoder", "trailing_incomplete_frame"));
            }
            self.terminal = true;
            self.finish_capture(true);
        }
        Some(chunk)
    }

    fn fail_at(&mut self, stage: &str, kind: &str) -> Bytes {
        self.error_sent = true;
        if let Some(capture) = self.stream_capture.as_mut() {
            capture.malformed(stage, kind);
        }
        if let Some(traffic) = self.traffic.as_ref() {
            traffic.write_json(
                "060-messages-stream-error",
                &serde_json::json!({
                    "stage": stage,
                    "kind": kind,
                    "bytes": self.bytes,
                    "chunks": self.chunks,
                }),
            );
        }
        let value = serde_json::json!({
            "type": "error",
            "error": {
                "type": "api_error",
                "message": format!("{} Messages stream is invalid", self.provider)
            }
        });
        if let Some(capture) = self.stream_capture.as_mut() {
            capture.downstream_event("error", value.clone());
        }
        self.finish_capture(false);
        Bytes::from(encode_sse_event(Some("error"), &value.to_string()))
    }

    fn finish_capture(&mut self, completed: bool) {
        if let (Some(capture), Some(traffic)) = (self.stream_capture.take(), self.traffic.as_ref())
        {
            capture.finish_named(
                traffic,
                serde_json::json!({
                    "kind": if completed { "stream_completion" } else { "stream_error" },
                    "bytes": self.bytes,
                    "chunks": self.chunks,
                }),
                "061-messages-stream-summary",
            );
        }
    }
}

impl<S> Drop for MessagesStreamState<S> {
    fn drop(&mut self) {
        if self.terminal || self.stream_capture.is_none() {
            return;
        }
        if let (Some(capture), Some(traffic)) = (self.stream_capture.take(), self.traffic.as_ref())
        {
            capture.finish_named(
                traffic,
                serde_json::json!({
                    "kind": "stream_abandoned",
                    "reason": "downstream_body_dropped",
                    "bytes": self.bytes,
                    "chunks": self.chunks,
                }),
                "061-messages-stream-summary",
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use http_body_util::BodyExt;

    fn delayed_stream(chunks: Vec<(Duration, &'static str)>) -> Body {
        let upstream = futures_util::stream::unfold(chunks.into_iter(), |mut chunks| async move {
            let (delay, chunk) = chunks.next()?;
            tokio::time::sleep(delay).await;
            Some((
                Ok::<_, std::io::Error>(Bytes::from_static(chunk.as_bytes())),
                chunks,
            ))
        });
        stream_body(Box::pin(upstream), "Test", None, "req".into(), None)
    }

    #[tokio::test(start_paused = true)]
    async fn silent_stream_sends_repeated_pings_then_completes() {
        let mut body = delayed_stream(vec![
            (
                Duration::from_secs(31),
                "event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
            ),
            (
                Duration::from_secs(31),
                "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
            ),
        ]);
        for expected in [
            "ping",
            "ping",
            "message_start",
            "ping",
            "ping",
            "message_stop",
        ] {
            let data = body.frame().await.unwrap().unwrap().into_data().unwrap();
            assert!(String::from_utf8_lossy(&data).contains(&format!("event: {expected}\n")));
        }
        assert!(body.frame().await.is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn heartbeat_never_splits_an_upstream_frame() {
        let body = delayed_stream(vec![
            (Duration::ZERO, "event: message_stop\ndata: {\"type\":"),
            (Duration::from_secs(31), "\"message_stop\"}\n\n"),
        ]);
        let bytes = body.collect().await.unwrap().to_bytes();
        assert_eq!(
            bytes.as_ref(),
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn silent_stream_without_terminal_event_still_fails() {
        let body = delayed_stream(vec![(
            Duration::from_secs(31),
            "event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
        )]);
        let bytes = body.collect().await.unwrap().to_bytes();
        let text = String::from_utf8_lossy(&bytes);
        assert_eq!(text.matches("event: ping\n").count(), 2);
        assert!(text.contains("Test Messages stream is invalid"));
        assert!(!text.contains("event: message_stop\n"));
    }

    #[tokio::test]
    async fn live_stream_rejects_an_incomplete_frame_after_message_stop() {
        let upstream =
            futures_util::stream::iter([Ok::<Bytes, std::io::Error>(Bytes::from_static(
                b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\ndata: {",
            ))]);
        let mut state = MessagesStreamState {
            provider: "Test",
            upstream,
            decoder: SseDecoder::default(),
            terminal: false,
            error_sent: false,
            monitor: None,
            req_id: "req".into(),
            bytes: 0,
            chunks: 0,
            stream_capture: None,
            traffic: None,
        };
        let output = state.next_output().await.expect("error event");
        assert!(String::from_utf8_lossy(&output).contains("Test Messages stream is invalid"));
    }

    #[tokio::test]
    async fn live_stream_rejects_a_complete_event_after_message_stop() {
        let upstream =
            futures_util::stream::iter([Ok::<Bytes, std::io::Error>(Bytes::from_static(
                concat!(
                    "event: message_stop\n",
                    "data: {\"type\":\"message_stop\"}\n\n",
                    "event: content_block_delta\n",
                    "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"late\"}}\n\n"
                )
                .as_bytes(),
            ))]);
        let mut state = MessagesStreamState {
            provider: "Test",
            upstream,
            decoder: SseDecoder::default(),
            terminal: false,
            error_sent: false,
            monitor: None,
            req_id: "req".into(),
            bytes: 0,
            chunks: 0,
            stream_capture: None,
            traffic: None,
        };
        let output = state.next_output().await.expect("error event");
        assert!(String::from_utf8_lossy(&output).contains("Test Messages stream is invalid"));
    }
}
