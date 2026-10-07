# Privacy and data handling

This source distribution contains no owner database, chat archive, biometric enrollment,
voice recording, trading ledger, API secret or device pairing. New installations start
with fresh local state. Do not commit `data/`, `.runtime/`, `.venv/`, `secrets/` or `.env`.

Local processing is the default for language inference and recognition. Explicit integrations
can transmit task inputs to their providers. Market data and news lookups contact external
sources. Review configuration before enabling external providers or Telegram. The local HTTP
service is designed for one trusted computer, not public Internet hosting or shared accounts.

New face enrollment stores biometric vectors under the local data directory, not the source
tree. The Vision app provides deletion and keeps camera frames in memory. It sends a single
frame to loopback Ollama only when requested. Do not attach vectors or private images to issues.

Stop the app before backing up or deleting its data. Backups you create may retain deleted
information. There is no hosted telemetry or analytics service configured by this release.
Dependencies and optional providers have their own privacy policies.
