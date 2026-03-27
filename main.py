"""
Dossier API - Toelatingen beschermd erfgoed.

Run with: uvicorn main:app --reload
"""

from gov_dossier_engine import create_app

app = create_app(config_path="gov_dossier_app/config.yaml")
