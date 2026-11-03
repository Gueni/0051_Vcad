// The server watches this connection, rather than expiring JS heartbeats in
// inactive tabs. TCP closure also covers missing pagehide events and crashes.
export function trackViewerSession() {
  let source = null, stopped = false;
  const connect = () => {
    if (!stopped && !source) source = new EventSource('/session');
  };
  const disconnect = () => { source?.close(); source = null; };
  window.addEventListener('pagehide', disconnect);
  window.addEventListener('pageshow', connect);
  connect();
  return { stop() { stopped = true; disconnect(); } };
}
