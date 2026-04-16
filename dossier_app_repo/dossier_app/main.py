"""
Dossier API deployment entry point — toelatingen beschermd erfgoed.

This module is the `app` object uvicorn serves. It builds the
FastAPI application by loading the workflow plugin(s) listed in
`config.yaml` and wiring them into the engine. The engine itself
knows nothing about the toelatingen plugin — the plugin is pulled
in at runtime via `PluginRegistry` based on the config's plugin list.

Run with:
    uvicorn dossier_app.main:app --reload
"""

from pathlib import Path

from dossier_engine import create_app


_CONFIG_PATH = Path(__file__).parent / "config.yaml"

app = create_app(config_path=str(_CONFIG_PATH))
