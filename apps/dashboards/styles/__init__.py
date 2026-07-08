"""Dashboard Styling Utilities.

This module provides utilities to load and inject CSS styles for the Alpaca Dashboard.
Supports external CSS files with fallback to inline styles.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

STYLES_DIR = Path(__file__).parent / "styles"


def load_css_file(filename: str = "dashboard.css") -> str:
    """Load CSS content from file."""
    css_path = STYLES_DIR / filename
    try:
        if css_path.exists():
            return css_path.read_text(encoding="utf8")
    except Exception:
        pass
    return ""


def inject_css(css: str | None = None) -> None:
    """Inject CSS styles into the Streamlit app.

    Args:
        css: Optional CSS string. If None, loads from external file.
    """
    if css is None:
        css = load_css_file()

    if css:
        st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def inject_dashboard_styles() -> None:
    """Inject all dashboard styles including Google Fonts."""
    # Add Google Fonts for Inter and JetBrains Mono
    fonts_css = """
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap');
    """
    st.markdown(f"<style>{fonts_css}</style>", unsafe_allow_html=True)

    # Load main dashboard styles
    inject_css()
