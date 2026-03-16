"""
ScreenOps: embedded screen-operation workflow for the ReTrace agent.

Ported from IQWorksAtlas screenops/ package.  Uses an injected vision model
(the ReTrace chat model) and a configurable coordinate finder (URL + model + API key,
OpenAI-compatible API).
finder (API key from settings).
"""

from app.tools.screenops.workflow import run_screen_workflow

__all__ = ["run_screen_workflow"]
