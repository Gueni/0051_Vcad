// The server watches this connection, rather than expiring JS heartbeats in
// inactive tabs. TCP closure also covers missing pagehide events and crashes.
export function trackViewerSession(onOpen) {
  let source = null, stopped = false;
  const connect = () => {
    if (stopped || source) return;
    source = new EventSource('/session');
    // Another Vcad launch ("Open with") handed documents to this viewer.
    if (onOpen) source.addEventListener('open', onOpen);
  };
  const disconnect = () => { source?.close(); source = null; };
  window.addEventListener('pagehide', disconnect);
  window.addEventListener('pageshow', connect);
  connect();
  return { stop() { stopped = true; disconnect(); } };
}
