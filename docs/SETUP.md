# Setup and troubleshooting

Use Python 3.11 or newer. `scripts/setup.sh` creates a project-local virtual environment.
After dependency installation, start `.venv/bin/python run.py`. The default port is 8767.
If occupied, use `--port 8890`. Use `--data-dir /your/private/folder` for an isolated data root.
Keep local settings and all generated data outside version control.

For an Ollama connection error, start Ollama, check `ollama list` and install the selected model.
For missing camera models, run `scripts/download_vision_models.py` in repositories that include
it. A permission denial must be resolved in browser/OS settings. No camera stream is available
until the user starts one. Network market data can be unavailable independently of the server.

If macOS login startup was installed, keep the repository at the same location. Logs are in
`data/logs/`. Remove only this project's launcher using `scripts/install_autostart.py --remove`.
Restart after changing source or configuration. Back up local records before migrations.

This first release has no automatic updater. Download a new release into a separate folder,
review changes and migrate your own records deliberately. Do not copy someone else's data.
