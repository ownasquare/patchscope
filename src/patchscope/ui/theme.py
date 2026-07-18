"""Accessible, theme-aware visual contract for the PatchScope workbench."""

from __future__ import annotations

import streamlit as st

APP_STYLES = """
<style>
  :root {
    --ps-accent: #1f6f78;
    --ps-line: color-mix(in srgb, currentColor 17%, transparent);
    --ps-line-strong: color-mix(in srgb, currentColor 28%, transparent);
    --ps-surface: color-mix(in srgb, currentColor 3%, transparent);
    --ps-subtle: color-mix(in srgb, currentColor 5%, transparent);
    --ps-focus: #3b82f6;
    --ps-radius: 0.55rem;
  }

  [data-testid="stHeader"] { background: transparent; }
  [data-testid="stMainBlockContainer"] {
    max-width: 88rem;
    padding-top: 1.5rem;
    padding-bottom: 4rem;
  }
  [data-testid="stSidebar"] {
    border-right: 1px solid var(--ps-line);
  }

  h1, h2, h3, h4 { letter-spacing: -0.018em; }
  h1 { font-size: clamp(1.65rem, 3vw, 2rem) !important; }
  h2 { font-size: 1.35rem !important; }
  h3 { font-size: 1.08rem !important; }

  [data-testid="stVerticalBlockBorderWrapper"],
  [data-testid="stMetric"] {
    background: var(--ps-surface);
    border: 1px solid var(--ps-line) !important;
    border-radius: var(--ps-radius);
    box-shadow: 0 1px 2px color-mix(in srgb, var(--ps-ink) 6%, transparent);
  }
  [data-testid="stMetric"] { padding: 0.75rem 0.85rem; }
  [data-testid="stMetricLabel"] { font-weight: 620; }

  [data-testid="stTabs"] [data-baseweb="tab-list"] {
    border-bottom: 1px solid var(--ps-line);
    gap: 0.2rem;
  }
  [data-testid="stTabs"] button[role="tab"] {
    min-height: 2.75rem;
    color: var(--ps-muted);
  }
  [data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
    color: var(--ps-ink);
    font-weight: 650;
  }

  div.stButton > button,
  div.stFormSubmitButton > button,
  div.stDownloadButton > button,
  [data-testid="stLinkButton"] a {
    min-height: 2.75rem;
    border-radius: 0.48rem;
    font-weight: 640;
    box-shadow: none;
  }
  div.stButton > button[kind^="primary"],
  div.stFormSubmitButton > button[kind^="primary"] {
    background: var(--ps-accent);
    border-color: var(--ps-accent);
  }
  div.stButton > button[kind^="primary"] *,
  div.stFormSubmitButton > button[kind^="primary"] * { color: #ffffff !important; }

  [data-testid="stTextInputRootElement"],
  [data-testid="stTextAreaRootElement"],
  [data-baseweb="select"] > div,
  [data-testid="stFileUploaderDropzone"] {
    border-color: var(--ps-line-strong);
    border-radius: 0.48rem;
  }
  [data-testid="stFileUploaderDropzone"] { background: var(--ps-subtle); }
  [data-testid="stExpander"] {
    background: color-mix(in srgb, var(--ps-surface) 92%, transparent);
    border-color: var(--ps-line);
    border-radius: var(--ps-radius);
  }
  [data-testid="stCode"] {
    border: 1px solid var(--ps-line);
    border-radius: 0.45rem;
    overflow-x: auto;
  }
  [data-testid="stCode"] pre { overflow-x: auto; white-space: pre; }

  button:focus-visible,
  a:focus-visible,
  input:focus-visible,
  textarea:focus-visible,
  [role="radio"]:focus-visible,
  [role="tab"]:focus-visible,
  [role="button"]:focus-visible,
  summary:focus-visible {
    outline: 3px solid var(--ps-focus) !important;
    outline-offset: 2px !important;
  }
  [data-baseweb="input"] > div:focus-within,
  [data-baseweb="textarea"] > div:focus-within,
  [data-baseweb="select"] > div:focus-within,
  [data-testid="stFileUploaderDropzone"]:focus-within {
    border-color: var(--ps-focus) !important;
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--ps-focus) 28%, transparent) !important;
  }

  hr { border-color: var(--ps-line) !important; }

  @media (max-width: 48rem) {
    [data-testid="stMainBlockContainer"] { padding: 1rem 0.8rem 3rem; }
    [data-testid="stHorizontalBlock"] { gap: 0.6rem; }
    [data-testid="stMetric"] { padding: 0.65rem; }
    [data-testid="stTabs"] button[role="tab"] { padding-inline: 0.7rem; }
  }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      scroll-behavior: auto !important;
      transition-duration: 0.01ms !important;
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
    }
  }
</style>
"""


def apply_theme() -> None:
    """Apply static CSS only; no API or source text enters this style block."""

    st.html(APP_STYLES)


__all__ = ["APP_STYLES", "apply_theme"]
