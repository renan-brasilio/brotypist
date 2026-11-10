//! Ephemeral inference for a caller-owned Welcome field. No native capture,
//! history, clipboard, screen references, settings writes or external insertion.
use crate::{
    config::Config,
    core::{Snapshot, SnapshotKey, SuggestionState},
    features,
    process_client::WorkerClient,
    providers::GenerationOptions,
    runtime::{Clock, MonotonicClock, Worker},
};
use anyhow::{Result, ensure};
use serde_json::{Value, json};
use std::time::{Duration, Instant};

struct Request {
    id: u64,
    key: SnapshotKey,
    generation: u64,
    proceeded: bool,
}

#[cfg(feature = "native-smoke")]
#[derive(Default, serde::Serialize)]
struct SmokeEvents {
    loading: u64,
    ready: u64,
    results: u64,
    finals: u64,
    done: u64,
    errors: u64,
    without_request: u64,
    rejected_authority: u64,
    last_result_scalars: usize,
}

#[cfg(feature = "native-smoke")]
#[derive(Default)]
struct SmokeProgress {
    polls: u64,
    submissions: u64,
    proceed_calls: u64,
    cancellation_calls: u64,
    events: SmokeEvents,
    request_id: Option<u64>,
    last_poll: Option<Instant>,
    submitted: Option<Instant>,
    ready: Option<Instant>,
    proceeded: Option<Instant>,
    authorized_key: Option<(SnapshotKey, u64)>,
    first_result: Option<Instant>,
    terminal: Option<Instant>,
}
/// Calls are serialized by the widget owner. Returned acceptance text is only a
/// proposal; update must confirm exact native-widget readback before accounting.
pub struct PreviewSession {
    pub config: Config,
    worker: Box<dyn Worker>,
    clock: Box<dyn Clock>,
    snapshot: Snapshot,
    suggestion: SuggestionState,
    request: Option<Request>,
    attempted: Option<SnapshotKey>,
    expected: Option<Snapshot>,
    changed: Instant,
    accepted: u64,
    error: String,
    active: bool,
    owner_released: bool,
    closed: bool,
    stopped: bool,
    #[cfg(feature = "native-smoke")]
    smoke: SmokeProgress,
}
impl PreviewSession {
    pub fn new(config: Config) -> Result<Self> {
        Self::with_worker(
            config,
            Box::new(WorkerClient::new()?),
            Box::new(MonotonicClock),
        )
    }
    pub fn with_worker(
        mut config: Config,
        mut worker: Box<dyn Worker>,
        clock: Box<dyn Clock>,
    ) -> Result<Self> {
        config.validate()?;
        worker.set_idle_timeout(config.idle_release_seconds);
        let changed = clock.now();
        Ok(Self {
            config,
            worker,
            clock,
            snapshot: Snapshot {
                app_id: "brotypist.preview".into(),
                field_id: "welcome-try-it".into(),
                ..Default::default()
            },
            suggestion: SuggestionState::new(),
            request: None,
            attempted: None,
            expected: None,
            changed,
            accepted: 0,
            error: String::new(),
            active: true,
            owner_released: false,
            closed: false,
            stopped: false,
            #[cfg(feature = "native-smoke")]
            smoke: SmokeProgress::default(),
        })
    }
    pub fn set_owner_released(&mut self, released: bool) {
        self.owner_released = released;
        if !released {
            #[cfg(feature = "native-smoke")]
            {
                self.smoke.cancellation_calls += 1;
            }
            let _ = self.worker.cancel();
            self.request = None;
            self.suggestion.invalidate();
            self.expected = None;
        }
    }
    /// Focus loss and marked-text/IME composition must call set_active(false).
    pub fn set_active(&mut self, active: bool) {
        if self.active == active {
            return;
        }
        self.active = active;
        #[cfg(feature = "native-smoke")]
        {
            self.smoke.cancellation_calls += 1;
        }
        let _ = self.worker.cancel();
        self.request = None;
        self.expected = None;
        self.suggestion.invalidate();
        self.attempted = None;
        self.changed = self.clock.now();
    }
    pub fn update(&mut self, text: &str, caret: usize, selection_length: usize) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        let count = text.chars().count();
        ensure!(
            caret <= count && selection_length <= count - caret,
            "Preview cursor must describe the current owned document."
        );
        let mut current = self.snapshot.clone();
        current.text = text.into();
        current.caret = caret;
        current.selection_length = selection_length;
        if current.same_key(&self.snapshot) {
            return Ok(());
        }
        #[cfg(feature = "native-smoke")]
        {
            self.smoke.cancellation_calls += 1;
        }
        self.worker.cancel()?;
        self.request = None;
        let confirmed = self.expected.as_ref().is_some_and(|expected| {
            expected.same_key(&current) && expected.same_authority(&current)
        });
        if confirmed {
            self.accepted += 1;
            self.suggestion.snapshot = Some(current.clone());
        } else if !self.suggestion.advance(&current) {
            self.suggestion.invalidate();
        }
        self.expected = None;
        self.snapshot = current;
        self.attempted = (!self.suggestion.text.is_empty()).then(|| self.snapshot.key());
        self.changed = self.clock.now();
        self.error.clear();
        Ok(())
    }
    fn eligible(&self) -> bool {
        self.active
            && self.snapshot.eligible(self.config.min_chars as usize)
            && (self.config.complete_inside_text || self.snapshot.suffix().is_empty())
            && (self.config.completion_length != "word"
                || features::mid_word(self.snapshot.prefix()))
    }
    pub fn poll(&mut self) -> Value {
        #[cfg(feature = "native-smoke")]
        {
            self.smoke.polls += 1;
            self.smoke.last_poll = Some(self.clock.now());
        }
        if self.closed {
            let _ = self.close();
            return json!({"state":"idle","suggestion":"","message":"Preview closed.","accepted":self.accepted,"stopped":self.stopped});
        }
        for event in self.worker.poll() {
            #[cfg(feature = "native-smoke")]
            {
                let now = self.clock.now();
                let matches_submission = self.smoke.request_id == Some(event.id);
                let events = &mut self.smoke.events;
                match event.kind.as_str() {
                    "loading" => events.loading += 1,
                    "ready" => {
                        events.ready += 1;
                        if matches_submission {
                            self.smoke.ready = Some(now);
                        }
                    }
                    "result" => {
                        events.results += 1;
                        events.finals += u64::from(event.final_);
                        events.last_result_scalars = event.text.chars().count();
                        if matches_submission {
                            self.smoke.first_result.get_or_insert(now);
                        }
                    }
                    "done" => {
                        events.done += 1;
                        if matches_submission {
                            self.smoke.terminal = Some(now);
                        }
                    }
                    "error" => events.errors += 1,
                    _ => {}
                }
            }
            let Some(mut request) = self.request.take() else {
                #[cfg(feature = "native-smoke")]
                {
                    self.smoke.events.without_request += 1;
                }
                continue;
            };
            if event.id != request.id
                || request.key != self.snapshot.key()
                || request.generation != self.suggestion.generation
                || !self.active
                || !self.owner_released
            {
                #[cfg(feature = "native-smoke")]
                {
                    self.smoke.events.rejected_authority += 1;
                }
                if event.id != request.id {
                    self.request = Some(request);
                }
                continue;
            }
            match event.kind.as_str() {
                "ready" => {
                    if self.expected.is_none()
                        && self.eligible()
                        && self.worker.proceed(request.id).is_ok()
                    {
                        #[cfg(feature = "native-smoke")]
                        {
                            self.smoke.proceed_calls += 1;
                            if self.smoke.proceeded.is_none() {
                                self.smoke.proceeded = Some(self.clock.now());
                                self.smoke.authorized_key =
                                    Some((request.key.clone(), request.generation));
                            }
                        }
                        request.proceeded = true;
                        self.request = Some(request);
                    }
                }
                "result" if request.proceeded => {
                    let text = if self.config.completion_length == "word" {
                        features::current_word_only(self.snapshot.prefix(), &event.text)
                    } else {
                        event.text
                    };
                    self.suggestion
                        .offer(request.generation, self.snapshot.clone(), text);
                    if !event.final_ {
                        self.request = Some(request);
                    }
                }
                "error" => {
                    self.error = "Local preview inference failed.".into();
                    self.suggestion.invalidate();
                }
                "done" => {}
                _ => self.request = Some(request),
            }
        }
        let eligible = self.eligible();
        let pending = self.expected.is_some();
        if eligible
            && !pending
            && self.owner_released
            && self.error.is_empty()
            && self.suggestion.text.is_empty()
            && self.request.is_none()
            && self.attempted.as_ref() != Some(&self.snapshot.key())
            && self.clock.now().duration_since(self.changed)
                >= Duration::from_millis(self.config.debounce_ms.into())
        {
            let mut config = self.config.clone();
            let (tokens, max_words) = features::length_profile(&config.completion_length);
            config.max_tokens = config.max_tokens.min(tokens);
            let options = GenerationOptions {
                instructions: match self.config.resolve_instructions("", "", false) {
                    Ok(instructions) => instructions,
                    Err(_) => {
                        self.error = "Preview writing preferences are invalid.".into();
                        return json!({"state":"error","suggestion":"","message":self.error,"accepted":self.accepted});
                    }
                },
                max_words,
                ..Default::default()
            };
            let start = self
                .snapshot
                .caret
                .saturating_sub(config.context_chars as usize);
            let prefix = self
                .snapshot
                .text
                .slice(start, self.snapshot.caret - start)
                .expect("validated preview range");
            let suffix: String = self
                .snapshot
                .suffix()
                .chars()
                .take(config.context_chars as usize)
                .collect();
            match self.worker.submit(&config, prefix, &suffix, &options) {
                Ok(id) => {
                    #[cfg(feature = "native-smoke")]
                    {
                        self.smoke.submissions += 1;
                        self.smoke.request_id = Some(id);
                        self.smoke.submitted = Some(self.clock.now());
                        self.smoke.ready = None;
                        self.smoke.proceeded = None;
                        self.smoke.authorized_key = None;
                        self.smoke.first_result = None;
                        self.smoke.terminal = None;
                    }
                    self.request = Some(Request {
                        id,
                        key: self.snapshot.key(),
                        generation: self.suggestion.generation,
                        proceeded: false,
                    });
                    self.attempted = Some(self.snapshot.key());
                }
                Err(_) => self.error = "Local preview inference could not be started.".into(),
            }
        }
        let suggestion = if !pending
            && eligible
            && self
                .suggestion
                .snapshot
                .as_ref()
                .is_some_and(|s| s.same_key(&self.snapshot) && s.same_authority(&self.snapshot))
        {
            self.suggestion.text.as_str()
        } else {
            ""
        };
        let (state, message) = if !self.error.is_empty() {
            ("error", self.error.clone())
        } else if !suggestion.is_empty() {
            ("ready", "Suggestion ready.".into())
        } else if self.request.is_some() {
            let status = self.worker.status();
            (
                "loading",
                status
                    .get("message")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("Warming up the selected model…")
                    .into(),
            )
        } else if eligible && !self.owner_released {
            (
                "waiting",
                "Releasing the typing model for this preview…".into(),
            )
        } else if self.accepted > 0 {
            (
                "idle",
                format!(
                    "Accepted {} suggestion{}.",
                    self.accepted,
                    if self.accepted == 1 { "" } else { "s" }
                ),
            )
        } else if eligible {
            ("waiting", "Waiting for a typing pause…".into())
        } else {
            ("idle", "Waiting for you to type…".into())
        };
        json!({"state":state,"suggestion":suggestion,"message":message,"accepted":self.accepted})
    }
    /// First matching ready event authorized by the normal consumer. Retained
    /// across final/done delivery, but never valid for a changed or revoked field.
    #[cfg(feature = "native-smoke")]
    pub(crate) fn smoke_proceeded_at(&self, current: &Snapshot) -> Option<Instant> {
        let (key, generation) = self.smoke.authorized_key.as_ref()?;
        if self.closed
            || !self.active
            || !self.owner_released
            || self.expected.is_some()
            || !self.eligible()
            || *generation != self.suggestion.generation
            || !key.matches(&self.snapshot)
            || !current.same_key(&self.snapshot)
        {
            return None;
        }
        self.smoke.proceeded
    }
    /// Content-free observation only: never polls, drains reports, retries, or
    /// authorizes work. Relative timings describe consumer delivery, not GPU work.
    #[cfg(feature = "native-smoke")]
    pub(crate) fn smoke_observation(&self) -> Value {
        let now = self.clock.now();
        let worker = self.worker.status();
        let offset = |at: Option<Instant>| {
            self.smoke
                .submitted
                .zip(at)
                .and_then(|(start, at)| at.checked_duration_since(start))
                .map(|elapsed| elapsed.as_micros())
        };
        let request = self.request.as_ref();
        json!({
            "polls":self.smoke.polls,
            "last_poll_age_us":self.smoke.last_poll.and_then(|at| now.checked_duration_since(at)).map(|elapsed| elapsed.as_micros()),
            "submissions":self.smoke.submissions,
            "proceed_calls":self.smoke.proceed_calls,
            "cancellation_calls":self.smoke.cancellation_calls,
            "delivered_events":self.smoke.events,
            "pending":request.is_some(),
            "pending_age_us":request.and(self.smoke.submitted).and_then(|at| now.checked_duration_since(at)).map(|elapsed| elapsed.as_micros()),
            "request_proceeded":request.map(|request| request.proceeded),
            "request_matches_snapshot":request.map(|request| request.key == self.snapshot.key()),
            "request_matches_generation":request.map(|request| request.generation == self.suggestion.generation),
            "attempted_current_snapshot":self.attempted.as_ref() == Some(&self.snapshot.key()),
            "eligible":self.eligible(),
            "owner_released":self.owner_released,
            "active":self.active,
            "acceptance_pending":self.expected.is_some(),
            "suggestion_scalars":self.suggestion.text.chars().count(),
            "accepted":self.accepted,
            "closed":self.closed,
            "stopped":self.stopped,
            "consumer_delivery_us":{
                "ready":offset(self.smoke.ready),
                "proceed_call":offset(self.smoke.proceeded),
                "first_result":offset(self.smoke.first_result),
                "done":offset(self.smoke.terminal)
            },
            "worker":{
                "state":worker["state"],
                "lifecycle":worker["lifecycle"],
                "present":worker["worker_pid"].as_u64().is_some(),
                "model_loaded":worker["model_loaded"],
                "initializing":worker["initializing"],
                "evaluating":worker["evaluating"],
                "awaiting_proceed":worker["awaiting_proceed"],
                "terminating":worker["terminating"],
                "owner_finished":worker["owner_finished"],
                "release_pending":worker["release_pending"],
                "release_complete":worker["release_complete"],
                "active_request_matches":request.map(|request| worker["active_id"].as_u64() == Some(request.id)),
                "latest_request_matches":request.map(|request| worker["latest_id"].as_u64() == Some(request.id)),
                "inference":worker["inference"],
                "lost_terminal_reports":worker["lost_terminal_reports"]
            }
        })
    }
    fn accept(&mut self, full: bool, visible: Option<&str>) -> String {
        if self.closed
            || !self.active
            || !self.owner_released
            || self.expected.is_some()
            || !self.eligible()
            || visible == Some("")
        {
            return String::new();
        }
        let text = self
            .suggestion
            .accept(
                &self.snapshot,
                full,
                self.config.trailing_space,
                features::include_punctuation(&self.config.punctuation_mode, full),
                visible,
            )
            .unwrap_or_default();
        if !text.is_empty() {
            #[cfg(feature = "native-smoke")]
            {
                self.smoke.cancellation_calls += 1;
            }
            let _ = self.worker.cancel();
            self.request = None;
            self.expected = self.suggestion.snapshot.clone();
        }
        text
    }
    pub fn accept_word(&mut self) -> String {
        self.accept(false, None)
    }
    pub fn accept_all(&mut self) -> String {
        self.accept(true, None)
    }
    /// Native widgets pass the exact fully drawn prefix; clipping never grants
    /// authority to accept a hidden word or a hidden continuation tail.
    pub fn accept_visible(&mut self, full: bool, visible: &str) -> String {
        self.accept(full, Some(visible))
    }
    pub fn dismiss(&mut self) {
        if self.closed {
            return;
        }
        #[cfg(feature = "native-smoke")]
        {
            self.smoke.cancellation_calls += 1;
        }
        let _ = self.worker.cancel();
        self.request = None;
        self.expected = None;
        self.suggestion.invalidate();
        self.attempted = Some(self.snapshot.key());
        self.error.clear();
    }
    /// Retryable, signal-only cleanup; never makes the normal worker resume while
    /// an isolated preview child is still alive.
    pub fn close(&mut self) -> Result<bool> {
        if !self.closed {
            self.closed = true;
            self.active = false;
            self.request = None;
            self.expected = None;
            self.suggestion.invalidate();
            self.snapshot.text = "".into();
            self.snapshot.caret = 0;
            self.snapshot.selection_length = 0;
        }
        self.worker.request_stop()?;
        self.stopped = self.worker.is_stopped();
        Ok(self.stopped)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker::WorkerEvent;
    use std::{cell::RefCell, rc::Rc};
    #[derive(Default)]
    struct State {
        events: Vec<WorkerEvent>,
        submits: usize,
        proceeds: Vec<u64>,
        cancelled: usize,
        stopped: bool,
        options: Option<GenerationOptions>,
    }
    struct FakeWorker(Rc<RefCell<State>>);
    impl Worker for FakeWorker {
        fn prepare(&mut self, _: &Config) -> Result<u64> {
            Ok(0)
        }
        fn submit(&mut self, _: &Config, _: &str, _: &str, o: &GenerationOptions) -> Result<u64> {
            let mut s = self.0.borrow_mut();
            s.submits += 1;
            s.options = Some(o.clone());
            Ok(s.submits as u64)
        }
        fn proceed(&mut self, id: u64) -> Result<()> {
            self.0.borrow_mut().proceeds.push(id);
            Ok(())
        }
        fn poll(&mut self) -> Vec<WorkerEvent> {
            std::mem::take(&mut self.0.borrow_mut().events)
        }
        fn cancel(&mut self) -> Result<()> {
            self.0.borrow_mut().cancelled += 1;
            Ok(())
        }
        fn release(&mut self) -> Result<crate::process_client::ReleaseTicket> {
            WorkerClient::new()?.release()
        }
        fn set_idle_timeout(&mut self, _: u32) {}
        fn status(&self) -> Value {
            json!({"message":"loading"})
        }
        fn request_stop(&mut self) -> Result<()> {
            Ok(())
        }
        fn is_stopped(&mut self) -> bool {
            self.0.borrow().stopped
        }
    }
    struct Time(Rc<RefCell<Instant>>);
    impl Clock for Time {
        fn now(&self) -> Instant {
            *self.0.borrow()
        }
    }
    fn session() -> (PreviewSession, Rc<RefCell<State>>, Rc<RefCell<Instant>>) {
        let s = Rc::new(RefCell::new(State::default()));
        let t = Rc::new(RefCell::new(Instant::now()));
        (
            PreviewSession::with_worker(
                Config::default(),
                Box::new(FakeWorker(s.clone())),
                Box::new(Time(t.clone())),
            )
            .unwrap(),
            s,
            t,
        )
    }
    fn event(id: u64, kind: &str, text: &str, final_: bool) -> WorkerEvent {
        WorkerEvent {
            id,
            kind: kind.into(),
            text: text.into(),
            final_,
            model_loaded: true,
            message: String::new(),
            metrics: None,
        }
    }
    #[test]
    fn selection_never_borrows_another_models_readiness() {
        let (mut p, s, t) = session();
        p.update("Dear team", 9, 0).unwrap();
        *t.borrow_mut() += Duration::from_secs(1);
        p.poll();
        assert_eq!(s.borrow().submits, 0);
        p.set_owner_released(true);
        p.poll();
        assert_eq!(s.borrow().submits, 1);
        assert!(s.borrow().proceeds.is_empty());
        s.borrow_mut().events.push(event(1, "ready", "", false));
        p.poll();
        assert_eq!(s.borrow().proceeds, vec![1]);
        let o = s.borrow().options.clone().unwrap();
        assert!(o.context.is_empty() && o.personalization.is_empty() && o.utility.is_empty());
    }
    #[test]
    fn only_exact_widget_readback_counts_and_dismiss_blocks_retry() {
        let (mut p, s, t) = session();
        p.set_owner_released(true);
        p.update("Dear team", 9, 0).unwrap();
        *t.borrow_mut() += Duration::from_secs(1);
        p.poll();
        s.borrow_mut().events.extend([
            event(1, "ready", "", false),
            event(1, "result", ", thank you", true),
        ]);
        p.poll();
        let text = p.accept_all();
        assert_eq!(text, ", thank you");
        assert_eq!(p.poll()["accepted"], 0);
        assert!(p.accept_word().is_empty());
        p.update("Dear team, thank you", 20, 0).unwrap();
        assert_eq!(p.poll()["accepted"], 1);
        p.dismiss();
        *t.borrow_mut() += Duration::from_secs(1);
        p.poll();
        assert_eq!(s.borrow().submits, 1);
    }
    #[test]
    fn stale_selection_and_focus_cancel_output_and_cleanup_retries() {
        let (mut p, s, t) = session();
        p.set_owner_released(true);
        p.update("Hello", 5, 0).unwrap();
        *t.borrow_mut() += Duration::from_secs(1);
        p.poll();
        p.update("Hello", 0, 5).unwrap();
        s.borrow_mut()
            .events
            .push(event(1, "result", " world", true));
        assert_eq!(p.poll()["suggestion"], "");
        p.set_active(false);
        assert!(p.accept_all().is_empty());
        assert!(!p.close().unwrap());
        s.borrow_mut().stopped = true;
        assert!(p.close().unwrap());
    }
    #[test]
    fn hidden_preview_and_interior_word_prefix_never_accept_unseen_text() {
        let (mut p, s, t) = session();
        p.set_owner_released(true);
        p.update("Hello", 5, 0).unwrap();
        *t.borrow_mut() += Duration::from_secs(1);
        p.poll();
        s.borrow_mut().events.extend([
            event(1, "ready", "", false),
            event(1, "result", " world tomorrow", true),
        ]);
        p.poll();
        assert!(p.accept_visible(true, "").is_empty());
        assert!(p.accept_visible(false, " wo").is_empty());
        assert_eq!(p.accept_visible(true, " world"), " world ");
        assert_eq!(p.poll()["accepted"], 0);
        p.update("Hello world ", 12, 0).unwrap();
        assert_eq!(p.poll()["accepted"], 1);
    }
    #[test]
    fn preview_uses_canonical_language_instructions_without_optional_sources() {
        let (mut preview, state, time) = session();
        preview.config.writing_language = "pt-BR".into();
        preview.config.spelling_language = "pt-PT".into();
        preview.config.custom_instructions = "  Preserve e\u{301} and espaços.\n".into();
        preview.config.use_history = true;
        preview.config.screen_context = true;
        preview.config.clipboard_context = true;
        preview.set_owner_released(true);
        preview.update("Olá equipa", 10, 0).unwrap();
        *time.borrow_mut() += Duration::from_secs(1);
        preview.poll();
        let options = state.borrow().options.clone().unwrap();
        assert_eq!(
            options.instructions,
            "Default writing language: pt-BR. Follow the surrounding text when it uses another language.\nDefault regional spelling and conventions: pt-PT.\n  Preserve e\u{301} and espaços.\n"
        );
        assert!(options.context.is_empty());
        assert!(options.personalization.is_empty());
        preview.set_owner_released(false);
        state.borrow_mut().events.extend([
            event(1, "ready", "", false),
            event(1, "result", " escrita", true),
        ]);
        assert_eq!(preview.poll()["suggestion"], "");
        assert!(state.borrow().proceeds.is_empty());
    }

    #[cfg(feature = "native-smoke")]
    #[test]
    fn smoke_observation_does_not_consume_or_authorize_preview_work() {
        let (mut preview, state, time) = session();
        let draft = "Private owned draft";
        let result = " unpublished continuation";
        preview.set_owner_released(true);
        preview.update(draft, draft.chars().count(), 0).unwrap();
        *time.borrow_mut() += Duration::from_secs(1);
        preview.poll();
        state.borrow_mut().events.push(event(1, "ready", "", false));
        let before = preview.smoke_observation();
        assert_eq!(before["pending"], true);
        assert_eq!(before["request_proceeded"], false);
        assert!(state.borrow().proceeds.is_empty());
        preview.poll();
        assert_eq!(state.borrow().proceeds, [1]);
        state.borrow_mut().events.extend([
            event(1, "result", result, true),
            event(1, "done", "", false),
        ]);
        let pending = preview.smoke_observation();
        assert_eq!(pending["pending"], true);
        assert_eq!(pending["request_proceeded"], true);
        assert_eq!(preview.poll()["suggestion"], result);
        let complete = preview.smoke_observation();
        assert_eq!(complete["pending"], false);
        let serialized = complete.to_string();
        for private in [
            draft,
            result,
            "\"request_id\"",
            "\"worker_pid\"",
            "\"active_id\"",
        ] {
            assert!(!serialized.contains(private));
        }
    }

    #[cfg(feature = "native-smoke")]
    #[test]
    fn smoke_observation_distinguishes_no_offer_from_pending_work() {
        let (mut preview, state, time) = session();
        preview.set_owner_released(true);
        preview.update("Hello", 5, 0).unwrap();
        *time.borrow_mut() += Duration::from_secs(1);
        preview.poll();
        state.borrow_mut().events.extend([
            event(1, "ready", "", false),
            event(1, "result", "", true),
            event(1, "done", "", false),
        ]);
        preview.poll();
        let observed = preview.smoke_observation();
        assert_eq!(observed["pending"], false);
        assert_eq!(observed["attempted_current_snapshot"], true);
        *time.borrow_mut() += Duration::from_secs(30);
        preview.poll();
        assert_eq!(state.borrow().submits, 1);
    }

    #[cfg(feature = "native-smoke")]
    #[test]
    fn readiness_requires_current_authority_and_survives_terminal_delivery() {
        let (mut preview, state, time) = session();
        preview.set_owner_released(true);
        preview.update("Hello", 5, 0).unwrap();
        let current = preview.snapshot.clone();
        *time.borrow_mut() += Duration::from_secs(1);
        preview.poll();
        state
            .borrow_mut()
            .events
            .push(event(99, "ready", "", false));
        preview.poll();
        assert!(preview.smoke_proceeded_at(&current).is_none());
        assert!(state.borrow().proceeds.is_empty());
        state.borrow_mut().events.push(event(1, "ready", "", false));
        assert!(preview.smoke_proceeded_at(&current).is_none());
        preview.poll();
        let authorized = preview.smoke_proceeded_at(&current).unwrap();
        *time.borrow_mut() += Duration::from_secs(1);
        state.borrow_mut().events.extend([
            event(1, "ready", "", false),
            event(1, "result", " world", true),
            event(1, "done", "", false),
        ]);
        preview.poll();
        assert_eq!(preview.smoke_proceeded_at(&current), Some(authorized));
        let mut changed = current.clone();
        changed.caret = 0;
        assert!(preview.smoke_proceeded_at(&changed).is_none());
        preview.set_owner_released(false);
        assert!(preview.smoke_proceeded_at(&current).is_none());
    }

    #[cfg(feature = "native-smoke")]
    #[test]
    fn revoked_ready_event_cannot_start_the_generation_budget() {
        let (mut preview, state, time) = session();
        preview.set_owner_released(true);
        preview.update("Hello", 5, 0).unwrap();
        let current = preview.snapshot.clone();
        *time.borrow_mut() += Duration::from_secs(1);
        preview.poll();
        state.borrow_mut().events.push(event(1, "ready", "", false));
        preview.set_active(false);
        preview.poll();
        assert!(preview.smoke_proceeded_at(&current).is_none());
        assert!(state.borrow().proceeds.is_empty());
    }
}
