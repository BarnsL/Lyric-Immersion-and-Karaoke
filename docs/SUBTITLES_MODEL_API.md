# Subtitles Mode And Model API

Subtitles mode is an explicit toggle/preset for videos, shows, concerts, and
agent-edited transcripts. It is not site detection.

## Behavior Contract

| Setting | Current behavior |
|---|---|
| Turning Subtitles on | Applies the Subtitles preset once: 45% opacity, 100% font, stationary bottom-center, all subtitle layers on. |
| Turning Subtitles off | Stops subtitle fetching/render behavior and returns to normal music handling. |
| Opacity / font size / position / display / scroll / performance | Normal app settings stay authoritative after the preset is applied. Changing these menus affects subtitle mode live. |
| GPU vs CPU renderer | Both read the same style state. Tk is the guaranteed CPU fallback; Tauri polls `/overlay` for GPU rendering. |
| Detection | Subtitles are never enabled by seeing Netflix, Crunchyroll, 9anime, animepahe, or another site name. A user, tray preset, or API/model must enable it. |
| Cache | Subtitle JSON files carry `meta.subtitle=true` and are skipped by the normal song library index. |

## Local API

The local API is `127.0.0.1:8765` and can be toggled from the tray. If
`KARAOKE_API_TOKEN` is set, pass it as `X-API-Token` or `?token=...`.

### Read subtitle state

```bash
curl "http://127.0.0.1:8765/subtitles?start=0&count=200"
```

The response includes:

| Field | Meaning |
|---|---|
| `mode` | `on` or `off`, the persistent subtitle toggle. |
| `active` | Whether subtitle behavior is currently engaged. |
| `settings` | Normal visual settings: opacity, font scale, position, scroll, scroll speed, subtitle display, layers, performance, display. |
| `meta` | Current loaded body metadata: title, artist, language, source, duration, subtitle flag. |
| `lines` | Editable timed transcript window. Each line has `i`, `t`, `start`, `end`, `jp`, `rm`, and `en`. |
| `schema` | Example patch/replace/settings payloads for an agent. |

### Toggle or apply preset

```bash
curl -X POST http://127.0.0.1:8765/subtitles ^
  -H "Content-Type: application/json" ^
  -d "{\"mode\":\"on\"}"
```

```bash
curl -X POST http://127.0.0.1:8765/subtitles ^
  -H "Content-Type: application/json" ^
  -d "{\"preset\":\"subtitles\"}"
```

### Change normal subtitle settings

```json
{
  "settings": {
    "opacity": 0.45,
    "font_scale": 1.0,
    "position": {"x": "center", "y": "bottom"},
    "scroll": "none",
    "scroll_speed": 200,
    "subtitle_display": "stationary",
    "layers": {"native": true, "romaji": true, "english": true},
    "performance": "smooth"
  }
}
```

`opacity` accepts `0.45` or `45`; `font_scale` accepts `1.0` or `100`.

### Patch individual lines

```json
{
  "mode": "on",
  "patch": [
    {"i": 12, "jp": "corrected subtitle", "rm": "corrected reading", "en": "corrected translation"}
  ],
  "persist": true
}
```

### Replace the transcript

```json
{
  "mode": "on",
  "replace": [
    {"t": [1.25, 4.50], "jp": "native text", "rm": "romanization", "en": "English"},
    {"start": 5.00, "end": 7.25, "jp": "next line", "rm": "", "en": ""}
  ]
}
```

### Shift all subtitle timings

```json
{"shift": 0.25}
```

Positive values make subtitle timestamps later. Negative values make them
earlier, clamped at zero.

## Safety And Validation

- POST bodies are capped at 2 MB.
- Line timings must be finite and satisfy `0 <= start < end`.
- Replacement lines must be monotonic by start time.
- Text fields are cleaned and capped.
- Line edits require subtitle mode to be on, either before the call or via the
  same payload.
- Saved subtitle edits go to subtitle-tagged JSON files, not normal song cache
  files, unless the currently loaded file is already a subtitle file.

## Code Ownership

| Task | Primary code |
|---|---|
| Tray preset/toggle | `main.py:apply_preset`, `main.py:set_subs_mode`, `main.py:set_subs_display` |
| Normal setting parity | `main.py:_effective_scroll`, `_effective_pos_x`, `_effective_pos_y`, `_effective_opacity`, setters like `set_opacity`, `set_font_scale`, `set_scroll`, `set_display` |
| Subtitle API read | `main.py:get_subtitles`, `api.py` `GET /subtitles` |
| Subtitle API write | `main.py:apply_subtitle_api_update`, `_apply_subtitle_settings_api`, `_coerce_subtitle_line`, `_save_subtitle_body`, `api.py` `POST /subtitles` |
| Caption fetch | `main.py:load_youtube_captions`, `_apply_captions`, `_subs_no_captions_fallback` |
| Renderer payload | `main.py:get_overlay_state`, Tauri `overlay/lyric-overlay.exe` polling `/overlay` |

## How To Modify

- To add a new subtitle visual setting, first add it to the normal tray/API
  setting path, then expose it in `get_subtitles()["settings"]`, then accept it
  in `_apply_subtitle_settings_api`.
- To add a new subtitle line field, update `_subtitle_line_payload`,
  `_coerce_subtitle_line`, `_save_subtitle_body`, and any renderer that consumes
  the field.
- To change the default preset, edit `_apply_subtitle_visual_defaults`; do not
  add hidden `_effective_*` overrides, because that makes tray settings appear
  broken.
- To add a model workflow, prefer one POST `/subtitles` payload that combines
  `mode`, `settings`, and `patch` or `replace`; this keeps UI-thread updates
  atomic from the user's point of view.

## How To Reverse

- Runtime rollback: turn Subtitles off from the tray or POST
  `{"mode":"off"}` to `/subtitles`.
- Visual rollback: apply the Gaming or Karaoke preset from the tray or POST
  `{"preset":"karaoke"}` to `/subtitles`.
- Bad transcript rollback: delete the matching `*-subtitles.json` file from the
  app data `lyrics/` folder, or use the existing cache purge tools for the
  currently loaded body.
- Code rollback for the model API: remove the `/subtitles` route entries and
  handler blocks from `api.py`, then remove the `get_subtitles` and
  `apply_subtitle_api_update` helper block from `main.py`.
- Code rollback for setting parity: restore the old hidden subtitle overrides in
  `_effective_scroll`, `_effective_pos_x`, `_effective_pos_y`, and
  `_effective_opacity`. This is not recommended because it makes normal tray
  settings stop affecting subtitle mode.

