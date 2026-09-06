// HermesRemote has no dashboard UI: the client is the Android app, and the plugin exists for its
// backend router alone. The manifest still declares an entry (the dashboard defaults to this path
// whether or not it is named), so this no-op file exists to keep the SPA from fetching a 404.
export default function register() {}
