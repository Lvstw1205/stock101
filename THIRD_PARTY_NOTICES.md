# Third-party components

Original project code: PolyForm Noncommercial 1.0.0, copyright 2026 Lvstw1205.
Dependencies installed through pip, model downloads and external services retain their own
licenses. This repository's noncommercial terms do not replace those upstream terms.

| Component | Purpose | Upstream license/source |
| --- | --- | --- |
| Python | Runtime | [PSF](https://docs.python.org/3/license.html) |
| Ollama | Optional model serving | [MIT](https://github.com/ollama/ollama/blob/main/LICENSE) |
| OpenCV / YuNet / SFace | Optional local face inference | [OpenCV Zoo](https://github.com/opencv/opencv_zoo); model notices in `third_party/` where included |
| MediaPipe Tasks Vision | Optional hand landmarks | [Apache-2.0](https://github.com/google-ai-edge/mediapipe/blob/master/LICENSE) |
| TradingAgents | Optional research backend | [Apache-2.0](https://github.com/TauricResearch/TradingAgents/blob/main/LICENSE) |

Only components relevant to an installation are used. Model weights are downloaded separately;
their provenance and hashes are in `config/vision-assets.json` where applicable. LLM weights
have model-specific terms. Private or unlicensed avatars, voice likenesses and model caches
from the original installation are not distributed.
