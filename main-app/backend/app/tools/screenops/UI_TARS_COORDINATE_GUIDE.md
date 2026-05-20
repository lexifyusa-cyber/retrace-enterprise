# UI-TARS Coordinate System Guide

This document explains how the ScreenOps tool handles UI-TARS (Qwen2-VL) coordinate specifications.

## Overview

UI-TARS models use Qwen2-VL's smart_resize, which requires specific image dimensions and outputs coordinates in a normalized 0-1000 scale format.

## Image Format Requirements

Screenshots must meet these constraints:

| Constraint | Value |
|-----------|-------|
| Both dimensions divisible by | 28 |
| Min total pixels | 78,400 (≈280×280) |
| Max total pixels | 12,845,056 (≈3584×3584) |
| Max aspect ratio | 200:1 |
| Recommended for desktop | 1120×784 or 1344×756 |

### Image Preprocessing

The `_resize_screenshot_for_coord_finder()` function automatically:

1. Scales down if exceeds max pixels
2. Scales up if below min pixels  
3. Rounds both dimensions to nearest multiple of 28
4. Ensures minimum 28-pixel dimension

**Example:**
```
Input: 1920×1080 screenshot
→ Resize to 1344×756 (both divisible by 28, matches 16:9 aspect ratio)
→ Send to model with dimensions provided in prompt
```

## Coordinate System

### Input Format (Model Output)

UI-TARS models output normalized coordinates on a **0-1000 scale**:

```
<point>452 310</point>
```

Where:
- `452` = horizontal position (0 = left edge, 1000 = right edge)
- `310` = vertical position (0 = top edge, 1000 = bottom edge)

This example means 45.2% horizontal, 31% vertical.

### Denormalization Process

Converting from model output to screen pixels:

```python
# Step 1: Get normalized coordinates from <point>x y</point>
norm_x, norm_y = 452, 310

# Step 2: Denormalize to resized image pixels
px_resized = (norm_x / 1000.0) * resized_w  # 452/1000 * 1344
py_resized = (norm_y / 1000.0) * resized_h  # 310/1000 * 756

# Step 3: Scale to actual screen pixels
scale_x = screen_width / resized_w      # 1920 / 1344
scale_y = screen_height / resized_h     # 1080 / 756
px = px_resized * scale_x
py = py_resized * scale_y

# Result: Final screen coordinates
# (608, 414) on 1920×1080 screen
```

The `_denormalize_ui_tars_coordinates()` function handles this automatically.

## System Prompts

### For Coordinate Finder (OpenAI-Compatible APIs)

```python
SYSTEM_PROMPT_COORDINATE_FINDER_AGENT = """
You are a GUI agent coordinate finder. Locate the CENTER of the target element in the screenshot.

The image is {screen_width} x {screen_height} pixels. You must output normalized coordinates on a 0-1000 scale.

TARGET: {target_description}

COORDINATE SYSTEM:
- X: 0 = left edge, 1000 = right edge
- Y: 0 = top edge, 1000 = bottom edge
- Example: If element center is at 45% horizontal and 31% vertical, output <point>450 310</point>

OUTPUT FORMAT (UI-TARS):
<point>450 310</point>
or if not visible:
<point>null null</point>
"""
```

### For Vision Models (if using pure UI-TARS)

```python
SYSTEM_PROMPT_UI_TARS_AGENT = """You are a GUI agent. Given a task and screenshot, perform actions.

For each action, output:
Thought: <your reasoning>
Action: <action>

Actions:
- click(start_box='<point>x y</point>')
- type(content='text to type')
- scroll(start_box='<point>x y</point>', direction='down', step=3)
- finished(content='task complete')

Coordinate system:
- Values in <point>x y</point> are normalized 0-1000 relative to image dimensions
- x: 0 = left, 1000 = right
- y: 0 = top, 1000 = bottom

Objective: {objective}
"""
```

## API Provider Handling

The tool automatically selects the appropriate format:

- **Anthropic API**: Uses JSON format `{"x": 123, "y": 456}` (backward compatible)
- **OpenAI-Compatible APIs**: Uses UI-TARS format `<point>452 310</point>`

## Response Parsing

The coordinate finder response parser:

1. **First attempt**: Looks for `<point>X Y</point>` (UI-TARS format)
2. **Fallback**: Looks for JSON `{"x": 123, "y": 456}` (for non-UI-TARS APIs)
3. **Clamps** to screen bounds: `[0, screen_width-1] × [0, screen_height-1]`

## Example Workflow

```
1. Capture screenshot: 1920×1080
2. Resize for model: 1344×756 (meets UI-TARS requirements)
3. Send to UI-TARS model with target description
4. Model responds: <point>874 623</point>
5. Denormalize: 
   - Resized pixel: (874/1000 * 1344, 623/1000 * 756) = (1175, 471)
   - Screen pixel: (1175 * 1920/1344, 471 * 1080/756) = (1678, 673)
6. Clamp to bounds: [0, 1919] × [0, 1079] → (1678, 673) ✓
7. Execute click at (1678, 673)
```

## Debug Logging

The tool logs detailed coordinate transformations to `screenops_debug.log`:

```
[2026-04-10T...] [coord_finder] target='Submit button' format='ui_tars_point' normalized=(874, 623) screen_pixels=(1678, 673)
```

## Compatibility Notes

- **UI-TARS models**: Use OpenAI-compatible API endpoints (e.g., vLLM, local Qwen2-VL)
- **Claude/Anthropic**: Falls back to JSON format automatically
- **Vision models**: Current implementation uses hybrid approach (vision outputs target descriptions, coordinator finder locates them)
