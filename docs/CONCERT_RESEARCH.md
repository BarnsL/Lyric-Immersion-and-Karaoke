# Concert Detection + Sync — Research & Roadmap

> **Purpose.** A living reference for the ongoing push to make Lyric Immersion
> reliably identify, transition between, and stay synced to songs across full
> concerts / 3D lives / medleys — including the awkward stuff between songs
> (MC, applause, intermission speech, tuning gaps).
>
> Not a spec. A worksheet. Each concert entry has a "Test log" block at the
> bottom you fill in as you run the app against it; each feature suggestion
> has an "Impact estimate / Status" line you flip as work lands.
>
> **Started:** 2026-07-04
> **Baseline app version at doc start:** v1.1.63
> **Living state doc:** [`HANDOVER-2026-07-04.md`](../HANDOVER-2026-07-04.md)
> **Auto-tune harness (parallel worktree):** [`D:\Lyric-Immersion-Auto`](../../Lyric-Immersion-Auto) — see [`auto/README.md`](../../Lyric-Immersion-Auto/auto/README.md).

---

## 1. Snapshot — how the app handles concerts today (v1.1.63)

### Five-layer pipeline

1. **Upfront classification.** `is_live_or_compilation(title, duration)` (main.py) uses title keywords (`concert / live / medley / ワンマン / ONE-MAN / anniversary`) plus duration > 10 min. `is_live_arrangement()` handles single-song live cuts (`LIVE MV / Short Ver / Acoustic / from …`). Concert-vs-regular changes the whole downstream policy.
2. **On-screen banner OCR.** `concert_ocr.py` reads the media-window pixels (via `ocr_lyrics.capture_source_window` / `PrintWindow`, occlusion-safe), fuzzy-matches against the local library. ≥ 0.85 match = load; uncached match = fetch cover-style. OCR is title-authoritative in concert context.
3. **Audio-based boundary detection.** `SongChangeDetector` (`songchange.py`) watches RMS + vocal-band ratio for applause gaps / silent windows; fires `_on_boundary` which triggers a whole-library `_decide_by_ear` inside a concert wrapper. `_check_applause_gap` skips offset writes during silence so the last-shown lyrics don't snap backward.
4. **Offline concert-audio analysis.** `concert_audio.py::analyze()` (shipped v1.1.57) downloads the video once, decodes to 16 kHz mono, builds an energy + vocal-tonality envelope, segments songs, localises vocal onsets, fingerprints each segment via `recognize.identify_pcm`, and returns a per-video plan (`starts / ends / onsets / shazam_ids / confidence`). `_apply_concert_plan` installs those onsets as lyric anchors and pipes the plan into `_concert_setlist_tick`.
5. **Live resync loop.** `_live_resync_loop` transcribes + matches to living audio on an 8× / 5× / 3× per-minute cadence (fast → holding → stable). Follows measured offset rather than resetting to studio timing. Waveform-gated to skip applause / instrumental segments.

Alongside those layers, `yt_description.py` parses chapters, description-embedded `MM:SS Title` setlists, and performer-credit blocks (originals / covers under `オリジナル曲 / COVER曲 / Setlist:` headers, emoji-bracketed items, `【Original】Song/Artist` inline markers).

### Concert-specific tuning already shipped

| Ticket / version | Feature |
|---|---|
| TICKET-022 | Concert song detection via on-screen banner OCR |
| TICKET-030 | Mode-aware sync: FOLLOW live/short arrangements; distrust repeated-chorus resets |
| TICKET-061 | Applause / cheering pause guard (`_check_applause_gap` skips offset writes) |
| TICKET-063 | 2-6 min "stale song" heuristic forces re-id; whole-library decide-by-ear on `_on_boundary` |
| TICKET-072 | Live/concert resync by ear (8× / 5× / 3× /min cadence, vocals-gated) |
| TICKET-079 (a,c) | Truncation-tolerant `_LIVE_VER_RE` (`3rd ONE`, `10th Anniversary`, `ワンマン` family); boundary schedules decide-by-ear ~12 s after Shazam inside a wrapper |
| TICKET-081 | Cover as live arrangement — FOLLOW offset, extended-intro fix |
| TICKET-106 | `_LIVE_VER_RE` expanded (JP idioms + Nth-ordinal truncation) |
| TICKET-112 | YouTube description credit + setlist extraction (`parse_setlist_timestamps`, `parse_song_candidates`) |
| TICKET-119 | Keep song head, reclassify `(from … ONE-MAN LIVE)` and `THE FIRST TAKE` as live arrangements |
| TICKET-122 | 2-tier ground truth: bundled unconditional / captions + OCR provisional until `_body_corroborated` |
| TICKET-121 | `metrics.py` concert bucketing (`_CONCERT_WINDOW_S`, `_CONCERT_FAIL_RATE`, `_CONCERT_SUCCESS_MAX`, `promote_concert()`) |
| TICKET-143 | Snap `_hi_offset = target` in live/concert so gold fill stays steady mid-line, re-anchors at boundary |
| TICKET-146 | Concert batch: OCR banner reading + fast lock + cover / live caption fusion |
| TICKET-149 | Eager 250 ms caption fetch for covers + live; skip energy correlator on covers |
| v1.1.37 | Concert-batch production deployment |
| v1.1.49 | Concert OCR reads the source window's pixels (PrintWindow), social-feed guard |
| v1.1.56 | OCR garbage guard: reject fuzzy-matches that align to open desktop-chrome window titles |
| v1.1.57 | Offline `concert_audio.py::analyze` pipeline |
| v1.1.62 | Chapter-intro hold; description-embedded setlists as native-chapter fallback; stale-lyrics clear on chapter switch; candidate-pool extractor |
| v1.1.63 | Hashtag pollution / CJK bracket over-extraction / date-chapter rejection / candidate-pool storage (Rise In Motion investigation) |

### Key file map

- [`main.py`](../main.py) — `is_live_or_compilation`, `is_live_arrangement`, `_LIVE_VER_RE`, `_concert_ocr_check`, `_apply_ocr_song`, `_fetch_ocr_song`, `_load_concert_setlist`, `_analyze_concert_audio`, `_apply_concert_plan`, `_concert_setlist_tick`, `_setlist_gen_check`, `_start_boundary`, `_on_boundary`, `_check_applause_gap`, `_live_resync_loop`, `_note_live_resync`, `_decide_by_ear`
- [`concert_ocr.py`](../concert_ocr.py) — `read_banner_lines`, `match_song`, `plausible_title`
- [`concert_audio.py`](../concert_audio.py) — `analyze`, `Segment`, energy envelope + segmentation + vocal-onset detection + fingerprinting
- [`yt_description.py`](../yt_description.py) — `parse_setlist_timestamps`, `parse_song_candidates`, credit-block extractor
- [`metrics.py`](../metrics.py) — concert bucketing rules + `promote_concert()`
- [`songchange.py`](../songchange.py) — `SongChangeDetector` RMS/vocal-ratio listener, applause-gap detection

### The known gap (highest-impact)

> **Candidate pool is extracted but not yet wired to a matching stage.**

`self._concert_candidates` is populated by `parse_song_candidates` on every concert load and logged, but no downstream code consumes it yet. Making it real is the last mile for livestream concerts with no chapter metadata (Rise In Motion class):

1. **Prefetch each candidate's lyrics** on load (background thread).
2. **Scope by-ear identification** (`_decide_by_ear`, `_live_resync_loop`) to this pool instead of blind whole-library — the pool is typically 5-15 songs vs the full library, so per-window accuracy should jump substantially.
3. **Route boundary decide-by-ear** the same way inside the concert wrapper.

Everything below assumes this lands.

### Other pending items (from HANDOVER + doc audits)

- **TICKET-063 (open)** — Whisper ASR fallback: transcribe → match against whole lyric library when OCR + Shazam both miss.
- **TICKET-079 (b,d)** — `decide_library_min` tuning for concert audio quality; setlist.fm mode + paste-in `MM:SS – Title` seeding.
- **Intermission / MC talk detection.** `_SETLIST_SKIP` already filters non-song chapters (`talk`, `after-talk`, `opening`, `encore`, `intermission`) by title, but no dedicated detection catches an UNLABELED MC segment. Energy-envelope gating is the obvious next primitive.
- **`concert_audio.py` tuning** — `_MAX_DUR_S = 4800`, `_MIN_SONG_S = 45.0`, `_MIN_GAP_S = 6.0`, `_ID_SLICE_S = 12.0`. Live-testing edge cases (very short songs, long MCs, instrumental intros) may reveal tuning needs.
- **ja-JP OCR pack**. `concert_ocr` degrades gracefully when the Japanese pack isn't installed but there's no in-app hint. Add tray hint / README note.
- **Concert-specific Whisper fine-tuning** (research / future).

---

## 2. Test corpus (5 concerts)

Each entry: video meta, best-known setlist (with confidence tier), failure-modes to hunt, and a per-run test-log table you fill in.

Corpus file format (for the auto-tune harness expansion — see §5): a YAML entry per concert under `auto/corpus/concerts.yaml` mirroring these fields. Ground-truth setlist times below become the golden anchors.

### Confidence tiers used below

- **high** — cross-verified from ≥ 2 independent sources (setlist.fm + Wikipedia + official channel).
- **medium** — single strong source (fan blog + official channel) or fuzzy-agreement across weaker sources.
- **low** — no verified setlist; entries are best-guess candidates only.
- **unknown** — no metadata retrievable in this pass.

---

### C1 — Rise In Motion (Todoroki Hajime birthday 3D live, 2026-06-07)

- **URL:** https://www.youtube.com/watch?v=Zcx1344XAmI
- **Title:** 【3DLIVE】Rise In Motion【轟はじめ】 #轟はじめ生誕祭2026
- **Artist / Channel:** 轟はじめ (Todoroki Hajime) — Hajime Ch. 轟はじめ ‐ ReGLOSS
- **Duration:** ~60-90 min (unverified)
- **Chapter markers:** none
- **MC / intermissions:** yes (musical-style 3D live with narration + story segments)
- **Setlist confidence:** **low** — no fan-transcribed setlist found; entries are catalog candidates, positions are **not** true play order.

> **This is the concert v1.1.63's hashtag / CJK-bracket / date-chapter / candidate-pool fixes were built for.** It's also the concert whose two YouTube chapters were `<Untitled Chapter 1>` + a merch-window date range. That means `parse_song_candidates` should light up here (originals list on Hajime's channel is well-formed), but every downstream identification depends on the candidate-pool → by-ear wiring landing.

| # | Title | Type | Notes |
|---|---|---|---|
| ~ | Deep Dive | original | Released 2026-06-08 (day after this live). Debut-at-birthday-live is the standard pattern. |
| ~ | BANCHO | original | Signature original. Very likely at a birthday live. |
| ~ | Countach | original | Solo original in active catalog. |
| ~ | BANZAI | original | Solo original in active catalog. |
| ~ | Dunk | original | 2026 solo release. |

Plus J-pop covers (near-certain but titles unknown from public sources).

**Failure modes to hunt:**
- Wrong-song for short titles (BANZAI / Dunk / Theater / Deep Dive) — high Shazam / NetEase collision rate.
- Cross-language collision — Korean/Chinese versions of "Deep Dive" / "Theater" / "BANZAI" outrank the JP release. The captions-side `_apply_captions` jp_vagency guard on `gpu-renderer` addresses part of this; the fetch path already does.
- Cover-artist mis-attribution — engine attributes covers to the VTuber instead of the original artist.
- MC / applause drift — the musical narration segments will linger the last-shown lyrics; expect wrong-song strike storms.
- Chorus-trap on BANCHO / Deep Dive (short repeated hooks).
- Generic chapter labels (`M1`, `ソング1`) — `concert_audio.py` corroboration must override.
- Energy-mode uncorroborated segments — the wordless HORNS RIOT-style dance interludes must **not** be fetched as "Song N".
- **Brand-new Deep Dive fingerprint** — `recognize.identify_pcm` has no prior sample; by-ear paths must not misroute to a same-title JP-Pop track.

**Sources.** [Hololive Pro talent page](https://hololive.hololivepro.com/en/talents/todoroki-hajime/) · [Wikipedia (JP)](https://ja.wikipedia.org/wiki/%E8%BD%9F%E3%81%AF%E3%81%98%E3%82%81) · [Preview (coki.jp)](https://coki.jp/article/column/83175/) · [Members-only encore video](https://www.youtube.com/watch?v=coev0bBUn-c)

**Test log**

| Date | App ver | What was shown correctly | What went wrong | Notes |
|---|---|---|---|---|
| 2026-07-04 | 1.1.63 | Rise In Motion diagnostics: chapter titles now cleaned, date-chapter filter live, candidate pool logged (per HANDOVER §7) | Actual per-song lyrics still not appearing on screen — candidate pool not wired to matching stage | Baseline before candidate-pool → decide-by-ear wiring lands |

---

### C2 — ReGLOSS 3D Live "Reach the top!" (2024-09-28)

- **URL:** https://www.youtube.com/watch?v=wIYvks57cQA
- **Title:** 【#ReGLOSS3Dライブ】Reach the top！
- **Artist / Channel:** hololive DEV_IS ReGLOSS
- **Duration:** ~1:04:00 (3840 s)
- **Chapter markers:** none
- **MC / intermissions:** yes (introductions between solo covers + group-talk block before encore)
- **Setlist confidence:** **medium** — Japanese fan-blog transcription; song titles cross-verified against uta-net / Hololive Pro / Apple Music. Per-song start times unavailable.

| # | Title | Type | Notes |
|---|---|---|---|
| 1 | 瞬間ハートビート | original (ReGLOSS) | Debut single, group |
| 2 | シンメトリー | original (ReGLOSS) | 2nd single, group |
| 3 | Shiny Smily Story | cover (hololive IDOL PROJECT) | Hiodoshi Ao solo |
| 4 | 未来のミュージアム | cover (Perfume) | Juufuutei Raden solo |
| 5 | SUPER DUPER | original (ReGLOSS) | Sub-unit (Ririka / Ao / Raden) |
| 6 | 泡沫メイビー | original (ReGLOSS) | group |
| 7 | Departures ～あなたにおくるアイの歌～ | cover (EGOIST) | Otonose Kanade solo. **Distinct from globe's 1996 "Departures".** |
| 8 | LAKI MODE | original (ReGLOSS) | Sub-unit (Kanade / Hajime) |
| 9 | BANDAGE | cover (Ayumu Imazu) | Todoroki Hajime solo. **Distinct from Lands / Kazuya Kamenashi 2010 J-film theme.** |
| 10 | bvdiz | original (ReGLOSS) | Read as "buddies"; group song from 1st album |
| 11 | フィーリングラデーション | original (ReGLOSS) | feelingradation — **exact katakana ↔ romaji cross-script fetch case ([[lyric-immersion-wrong-song-class]] memory).** |

**Failure modes to hunt:**
- Cross-language collision on the kana/kanji titles (`瞬間ハートビート`, `シンメトリー`, `泡沫メイビー`, `フィーリングラデーション`).
- **Native-title fetch bug**: `feelingradation` searches romaji, not `フィーリングラデーション` — that's the exact TICKET-126 case still open at the time of writing.
- Cover-original detection — 4 of 11 songs are covers, all with strong-collision title lookalikes. "Departures" ↔ globe collision is especially loud.
- Wrong-song for common titles: BANDAGE, Shiny Smily Story, SUPER DUPER, Departures.
- Chorus-trap on Shiny Smily Story (choruses ~identical to prior hololive renditions).
- MC drift on the solo-cover boundaries.
- Sub-unit sparse-vocal segments (SUPER DUPER / LAKI MODE) look like MC dips to the vocal-band gate.
- OCR mis-read of stylized katakana title card on `feelingradation` closer.

The URL carries `t=3112s` (~51:52) — that's the back third of the set (likely LAKI MODE / BANDAGE / bvdiz / feelingradation region). Good scrub-jump smoke.

**Sources.** [Fan-blog transcription](https://minkara.carview.co.jp/userid/318124/blog/48000489/) · [Hololive Pro video page](https://hololive.hololivepro.com/en/videos/13618/) · [ReGLOSS on Hololive wiki](https://hololive.wiki/wiki/Hololive_DEV_IS_ReGLOSS) · Track-level: [Apple Music (bvdiz)](https://music.apple.com/jp/song/bvdiz/1774207331), uta-net track pages

**Test log**

| Date | App ver | What was shown correctly | What went wrong | Notes |
|---|---|---|---|---|
| _pending_ | | | | |

---

### C3 — YOASOBI ARENA TOUR 2023 電光石火 @ Saitama Super Arena

- **URL:** https://www.youtube.com/watch?v=LQiPO0bhB9o
- **Title:** 【全場中字】『YOASOBI ARENA TOUR 2023 電光石火』2023.6.4@さいたまスーパーアリーナ
- **Artist / Channel:** YOASOBI (fan re-upload by pureland)
- **Duration:** ~110-130 min including MC + encore (unverified via oembed; typical for the tour)
- **Chapter markers:** none (fan re-upload has hardcoded 中字 subtitles instead)
- **MC / intermissions:** yes (long ikura MC blocks, encore break)
- **Setlist confidence:** **medium-high** — well-corroborated across setlist.fm + Wikipedia + multiple JP fan blogs; song identities certain, order verified, but start times must be measured (this reupload has no chapters).

| # | Title | Type | Notes |
|---|---|---|---|
| 1 | 祝福 (Shukufuku) | original | Opener. Gundam: The Witch from Mercury OP1 |
| 2 | 夜に駆ける (Yoru ni Kakeru) | original | Breakout hit — wrong-song magnet |
| 3 | 三原色 (Sangenshoku / RGB) | original |  |
| 4 | セブンティーン (Seventeen) | original |  |
| 5 | ミスター (Mister) | original | Common English title |
| 6 | 海のまにまに (Umi no Manima ni) | original |  |
| 7 | 好きだ (Suki da) | original | Extremely common JP title |
| 8 | 優しい彗星 (Yasashii Suisei) | original | BEASTARS S2 ED |
| 9 | もしも命が描けたら (Moshimo Inochi ga Egaketara) | original |  |
| 10 | たぶん (Tabun) | original | Very common title word |
| 11 | ハルジオン (Harujion / Halcyon) | original | Note: some EN sources mis-title as "Halcyon" |
| 12 | ハルカ (Haruka) | original | Common name/title |
| 13 | ツバメ (Tsubame) | original | NHK "Minna no Uta"; also has an official ミドリーズ collab — cover/original risk |
| 14 | 怪物 (Kaibutsu / Monster) | original | BEASTARS S2 OP |
| 15 | 群青 (Gunjou) | original | Massive singalong |
| 16 | アドベンチャー (Adventure) | original | Main-set closer |
| 17 | アイドル (Idol) | original | Encore. Oshi no Ko OP. Global chart hit |

**Failure modes to hunt:**
- Wrong-song for common titles (Mister / Suki da / Tabun / Haruka / Adventure).
- Cross-language collision — many popular ko / zh covers of Yoru ni Kakeru / Idol / Gunjou / Kaibutsu.
- Cover-original detection: Tsubame (official ミドリーズ collab + many kids-choir covers); Yasashii Suisei / Kaibutsu / Idol have countless anime-cover uploads.
- MC/applause drift — long ikura MC blocks between songs. If the engine holds the previous song's lyrics through the talk, it drifts through the next boundary.
- Wrong-song strike storms during VJ / intro / outro instrumentals between songs on a 17-track set.
- Chorus-trap on Yoru ni Kakeru / Idol / Gunjou (title-hook repeats — 2nd-confirm read matters).
- Encore boundary — long crowd-chant gap before Idol. Engine may stall on Adventure or auto-lock a wrong ID from ambient noise.
- **Chinese-subtitle burn-in on this reupload** — OCR-assisted sync will see zh characters mid-frame; may mis-categorize language or mis-anchor. Concert OCR banner reader must be robust to constant on-screen subs.
- No YouTube chapters — this concert leans hard on `concert_audio.py` offline vocal-onset + fingerprint. Great stress test for the corroboration threshold.

**Sources.** [setlist.fm](https://www.setlist.fm/setlist/yoasobi/2023/saitama-super-arena-saitama-japan-4ba6b38e.html) · [Wikipedia: Denkosekka Arena Tour](https://en.wikipedia.org/wiki/Denk%C5%8Dsekka_Arena_Tour) · [music-setlist.hatenablog](https://music-setlist.hatenablog.jp/entry/2023/06/04/170000) · [YOASOBI official live page](https://www.yoasobi-music.jp/live/49695) · [utaten](https://utaten.com/karaoke/yoasobi-setlist/)

**Test log**

| Date | App ver | What was shown correctly | What went wrong | Notes |
|---|---|---|---|---|
| _pending_ | | | | |

---

### C4 — Phase Connect "The OriginS of Stars" @ Offkai Expo Gen 4 (2025-06-20)

- **URL:** https://www.youtube.com/watch?v=Gl9B6D3ru7M
- **Title:** The OriginS of Stars - Offkai Expo Gen4 3D Concert
- **Artist / Channel:** Phase OriginS (Pipkin Pippa, Tenma Maemi, Rinkou Ashelia / Lia, Fujikura Uruka, Kaneko Lumi) — Phase Connect
- **Duration:** 43:22 (2602 s)
- **Chapter markers:** **yes** — 11 chapters (extracted via yt-dlp)
- **MC / intermissions:** yes ("After Talk" MC segment at 19:52)
- **Setlist confidence:** **medium** — chapter titles are authoritative, but several ("Kton Boogie", "La La La", "Fuura", "Face It") don't cross-verify against public setlist databases → probably intra-Phase originals or misheard covers. Best case for `concert_audio.py` offline-fingerprint corroboration.

| # | Title (chapter) | Type | Start | Notes |
|---|---|---|---:|---|
| 1 | Intro | other | 0:00 | Overture / opening sequence |
| 2 | Kton Boogie | ? | 3:25 | Likely 混沌ブギ / Konton Boogie (jon-YAKITORY, Project SEKAI) — romaji↔native mis-spelling |
| 3 | Fly To The World | ? | 7:08 | Not obviously indexed as a Phase original or well-known cover |
| 4 | La La La | ? | 11:06 | Extreme title collision |
| 5 | Next to You | ? | 15:41 | Extreme title collision |
| 6 | **After Talk** | **mc** | 19:52 | Explicit MC segment. `_SETLIST_SKIP` should catch by title. |
| 7 | Face It | ? | 22:38 | Section containing the URL-hinted `t=1575s` timestamp |
| 8 | Daddy Mama | ? | 26:25 | Possibly a play on Ado's "DADDY DADDY DO" |
| 9 | Remember You | ? | 29:45 | Common title |
| 10 | Fuura | ? | 33:55 | Almost certainly a tribute to former Phase member Fuura Yuri — not indexed as a song title |
| 11 | The Future | ? | 38:26 | Likely a Phase OriginS group original written for this concert |

Concert ends 43:22.

**Failure modes to hunt:**
- Wrong-song for extremely generic English titles ("La La La", "Next to You", "Remember You", "The Future", "Face It", "Daddy Mama") — will find dozens of unrelated tracks. **Ideal case for candidate-pool scoping.**
- Cross-language collision — EN VTubers frequently cover JP anime/vocaloid songs at 3D concerts. "Kton Boogie" (chapter) → 混沌ブギ (native) is the exact romaji-searches-not-native-title case ([[lyric-immersion-wrong-song-class]]).
- Cover-vs-original attribution — EN VTubers may sing JP covers; engine must not attribute the JP original artist as the performer.
- **MC segment drift** — "After Talk" (19:52-22:38) is 2:46 of no lyrics. If `_SETLIST_SKIP` misses the "After Talk" chapter title, engine will hold the previous song's lyrics through the boundary. Great test for MC / silence detection.
- **`Fuura` chapter-trap** — Fuura is a person, not a song. Engine will thrash searching for lyrics of a title that doesn't exist. Great test for "candidate pool as authoritative negative" wiring.
- Wrong-song strike storms during Intro (0-3:25) and After Talk (19:52-22:38).
- Chapter-corroboration override — v1.1.57 offline analysis was built exactly for the "chapter says X, offline fingerprint says Y" case; here at least half the chapter labels are placeholder-ish.

**Sources.** [Phase Connect on setlist.fm](https://www.setlist.fm/setlists/phase-connect-be725da.html) · [Offkai Expo event page](https://www.offkaiexpo.com/event/phase-connect-concert/) · [MusicBrainz event](https://musicbrainz.org/event/6e207a75-b820-4f53-9a0b-e7aebc3abc65) · [Kaneko Lumi discography](https://virtualyoutuber.fandom.com/wiki/Kaneko_Lumi/Discography) · [Fujikura Uruka discography](https://virtualyoutuber.fandom.com/wiki/Fujikura_Uruka/Discography) · [Konton Boogie on Project SEKAI wiki](https://projectsekai.fandom.com/wiki/Konton_Boogie)

**Test log**

| Date | App ver | What was shown correctly | What went wrong | Notes |
|---|---|---|---|---|
| _pending_ | | | | |

---

### C5 — Nirvana LIVE @ The Paramount, 1991-10-31 (4K remaster reupload)

- **URL:** https://www.youtube.com/watch?v=Z53nb74H7Vc
- **Title:** Nirvana LIVE @ The Paramount 1991 | Full Concert (4K Remastered)
- **Artist / Channel:** Nirvana (fan re-upload by fruzalv)
- **Chapter markers:** none
- **MC / intermissions:** yes (Kurt/Dave banter throughout; encore break; no formal intermission)
- **Setlist confidence:** **high** — cross-verified across setlist.fm, Wikipedia, livenirvana.com, and the official 2011 "Live at the Paramount" release. Track durations from the official release; start times estimated by cumulating durations + ~15-30 s per banter gap.

| # | Title | Type | Start ≈ | Notes |
|---|---|---|---:|---|
| 1 | Jesus Doesn't Want Me for a Sunbeam | cover (The Vaselines) | 0:30 | Kurt intro; ~5:48 |
| 2 | Aneurysm | original | 6:20 | ~5:05 |
| 3 | Drain You | original | 11:40 | ~5:16 |
| 4 | School | original | 17:10 | ~2:57 |
| 5 | Floyd the Barber | original | 20:25 | ~2:21 |
| 6 | Smells Like Teen Spirit | original | 23:20 | ~6:58; jam intro |
| 7 | About a Girl | original | 30:30 | ~3:02 |
| 8 | Polly | original | 33:50 | ~3:04 |
| 9 | Breed | original | 37:10 | ~2:54 |
| 10 | Sliver | original | 40:15 | ~2:18 |
| 11 | Love Buzz | cover (Shocking Blue) | 42:40 | ~4:01 |
| 12 | Lithium | original | 47:00 | ~6:02 |
| 13 | Been a Son | original | 53:20 | ~2:41 |
| 14 | Negative Creep | original | 56:15 | ~3:00 |
| 15 | On a Plain | original | 59:30 | ~4:09 |
| 16 | Blew | original | 1:03:50 | ~3:00; main-set close |
| 17 | *(Encore break / crowd)* | intermission | 1:07:10 | ~1 min |
| 18 | Rape Me | original | 1:08:10 | Early live version, ~3:04 |
| 19 | Territorial Pissings | original | 1:11:30 | ~2:55 |
| 20 | Endless, Nameless | original | 1:14:40 | ~7:39; introduced as "the secret song"; long noise-jam finale |

**Failure modes to hunt (Western rock / grunge test case):**
- **MC drift** — Kurt / Dave banter between nearly every song holds the last-shown title on the overlay and racks up false "still-playing" confirmations. **This is the classic MC-not-detected case; great smoke for whatever MC/silence handling gets built.**
- Wrong-song for common English words: School / Polly / Breed / Blew / Lithium.
- Cover-vs-original — "Jesus Doesn't Want Me for a Sunbeam" (Vaselines) and "Love Buzz" (Shocking Blue) — ID may return the original artist and either mislabel the performer or trigger `_body_corroborated=False` reject.
- **Endless, Nameless noise-jam tail** — extended dead-air-ish segment likely to generate strike storms against the last locked song.
- Chorus-trap on "Rape Me" and "Sliver" (title as the hook).
- **Encore gap dead-segment** — the ~1 min break between Blew and Rape Me is a classic dead-audio segment. Test whether the app holds vs re-identifies.
- Energy-mode uncorroborated segments — long crowd noise / tuning moments will look like separate segments to `concert_audio.py`'s vocal-band ratio detector but have no real song.

**Why this concert is in the corpus.** All four other corpora are Japanese VTuber concerts. Nirvana is the outlier — English rock, Western banter cadence, cover attribution, and the noise-jam finale — that tests whether concert handling generalizes beyond the VTuber convention. If v1.1.57's offline pipeline behaves the same way on this as on ReGLOSS, that's confirmation the primitives are actually generic.

**Sources.** [setlist.fm](https://www.setlist.fm/setlist/nirvana/1991/paramount-theatre-seattle-wa-53d67f05.html) · [livenirvana.com](https://www.livenirvana.com/concerts/91/91-10-31.php) · [Wikipedia: Live at the Paramount](https://en.wikipedia.org/wiki/Live_at_the_Paramount_(video)) · [nirvana.com](https://www.nirvana.com/releases-archive/live-at-the-paramount/)

**Test log**

| Date | App ver | What was shown correctly | What went wrong | Notes |
|---|---|---|---|---|
| _pending_ | | | | |

---

## 3. Failure-mode taxonomy

Recurring failure classes observed above, ranked by how many of the 5 concerts they show up in.

| Class | Concerts hit | What it looks like | Existing counter-measure | Gap |
|---|:-:|---|---|---|
| **MC / applause drift** (last-shown lyrics linger through non-song segments) | 5 / 5 | Overlay stays on song N through 30-180 s of talk, then thrashes into song N+1 | `_check_applause_gap` (silence + broadband), 2-6 min stale heuristic, `_SETLIST_SKIP` on chapter title | No **unlabeled** MC-segment detection; no energy-envelope gate on decide-by-ear during dead segments |
| **Cross-language collision** (ko/zh candidates for JP acts) | 4 / 5 | Korean-caption fan-track sneaks in for a ReGLOSS song | `fetch_lrc` jp_vagency + `_is_jp_act` on the caption/OCR paths (on `gpu-renderer` branch); confidence._KNOWN_JA table | Confidence table needs continual expansion; jp_vagency wiring on captions/OCR paths not yet on master |
| **Wrong-song for common titles** (Mister / La La La / Blew / Polly) | 5 / 5 | Global Shazam/fingerprint returns first-hit unrelated track | Candidate pool extraction (v1.1.63) | **Candidate pool not wired to matching stage** — top-priority feature |
| **Cover-original attribution** (VTuber sings J-pop, engine credits the VTuber) | 4 / 5 | Body-lyrics fetch under the wrong artist | `_is_cover` routes to live-FOLLOW; TICKET-149 eager captions | Robust cover→original artist resolution when the cover artist isn't in `_KNOWN_ORIGINAL_ARTISTS`; per-position chapter → cover-original hint from parsed setlist |
| **Chorus-trap** (repeated title-hook causes single-shot false-lock) | 4 / 5 | Same song locked as multiple different songs | v1.1.52 second-confirm read; `force_sync_span_s` | Concert-only aggressive 2nd-confirm before commit |
| **Generic chapter labels** (M1 / Song 1 / La La La) | 3 / 5 | Chapter tag overrides fingerprint | `concert_audio.py` offline corroboration (v1.1.57) | Corroboration threshold tuning per §1 pending |
| **Encore / dead-air segments** | 4 / 5 | Wrong-song strike storms during instrumentals | Vocal-band gate on `_live_resync_loop` | Non-vocal instrumental performance (e.g. band jam) currently confusable with dead air |
| **Chapter-trap** (chapter title isn't a song — "Fuura", "After Talk") | 2 / 5 | Engine thrashes searching for a non-song | `_SETLIST_SKIP` on known talk labels | Explicit "no-song" chapter type, plus candidate-pool authoritative-negative wiring |
| **Native-title vs romaji fetch** (フィーリングラデーション ↔ feelingradation) | 3 / 5 | Fetch searches romaji, misses native-title-only providers | TICKET-119 alias table (native / romaji / EN — well populated for known cases) | Auto-expand alias table from `parse_song_candidates` output on each concert load |
| **OCR banner mis-read (stylised text / hardcoded subs)** | 2 / 5 | OCR returns garbage or matches an on-screen subtitle | v1.1.49 PrintWindow source-window OCR; v1.1.56 window-title reject | Mid-frame subtitle burn-in (YOASOBI Chinese-subtitles case); stylised katakana title cards |
| **Brand-new song, no prior fingerprint** | 1 / 5 | Debut/reveal song at a birthday live — Shazam misses | (none — falls through to by-ear) | Candidate pool authoritative acceptance when title matches a known-pending release, plus deep-transcribe fallback |

---

## 4. Feature roadmap

Ranked P0 → P3. Each has a rough "signal" (what happens when it lands), an "impact" (which concerts / failure modes benefit), and a "size" hint.

### P0 — Wire the candidate pool to matching *(unlocks 3 concerts immediately)*

- **What.** Make `self._concert_candidates` authoritative for by-ear inside a concert wrapper: prefetch each candidate's lyrics on load; scope `_decide_by_ear` / `_live_resync_loop` / boundary decide-by-ear to the pool only.
- **Signal.** For any concert where `parse_song_candidates` returns a non-empty list, wrong-song rate drops precipitously; time-to-lock per song drops from tens of seconds to a few seconds.
- **Impact.** C1 (Rise In Motion — 100 % of value), C2 (ReGLOSS — reduces cross-language and native-title collisions), C4 (Phase Connect — chapter-trap on `Fuura` becomes a fast reject). Some benefit on C3 (YOASOBI) and C5 (Nirvana) via `parse_setlist_timestamps` output.
- **Size.** Small-to-medium — the extraction is already there; wiring points are `_decide_by_ear`, `_live_resync_loop`, `_on_boundary`, and the fetch path. New tune knob: `concert_pool_scoped=1`.
- **Auto-tune duel candidate.** `--a defaults --b concert-pool-scoped` on `concerts.yaml`.
- **Status.** ✅ **Landed in v1.1.64.** Tune knob `concert_pool_scoped=1` on by default; `concert_pool_prefetch_max=20`. New method `Overlay._prefetch_concert_candidates` (background fetch on `_load_concert_setlist`); `_decide_by_ear` adds every candidate's cached lyrics to the scoring pool inside a concert wrapper AND skips whole-library expansion when the scoped pool has a strong hit (`best_pool_score ≥ wrong_floor` AND beats `loaded_score + decide_margin`). Log lines start `decide-by-ear: … concert-scoped …` / `concert pool hit strong … skipping whole-library expansion` / `concert-candidates prefetch: cached +N …`.

### P1 — Unlabeled MC / silence-segment gate *(all 5 concerts benefit)*

> **PROPOSAL ONLY. NOT IMPLEMENTED.** Everything in this section is a design
> sketch. The knobs `mc_min_s`, `mc_vocal_ratio_ceiling`, `mc_speech_cadence_hz`
> and the state field `_in_mc_segment` **do not exist in main.py**. They are
> names this proposal suggests, not names you can use. Nothing reads them, `/tune`
> does not list them, and setting them has no effect. Do not write code, docs, or
> harness config that assumes any of them is available.

- **What.** Dedicated primitive that produces `is_mc_segment(t) → bool` from a rolling window of RMS + vocal-band ratio + speech-vs-song classifier (talk cadence vs sustained pitch). While True: hold the last-shown song, do NOT commit new offsets, do NOT accept new lyric fetches, escalate boundary-decide-by-ear only after the segment ends. Metric: seconds-of-drift-per-MC-segment.
- **Signal.** MC drift stops on C5 (Nirvana banter), C3 (YOASOBI ikura MC), C4 (After Talk 19:52). Fewer wrong-song strike storms during dead segments.
- **Impact.** All 5 concerts (MC drift is universal).
- **Size.** Medium. Would reuse `songchange.py` primitives + `_check_applause_gap`; would add a state field (`_in_mc_segment` is the proposed name) + tick-level policy, and would introduce tune knobs (proposed names: `mc_min_s / mc_vocal_ratio_ceiling / mc_speech_cadence_hz`). None of these exist yet. Requires labeling MC intervals in the corpus (~5-10 per concert × 5 concerts = 50 anchors).
- **Auto-tune duel candidate.** `--a defaults --b mc-gate-on --corpus concerts` scoring the new `mc_drift_s_total` metric.
- **Status.** *Not started.*

### P2 — Chapter-corroboration threshold tuning + no-song chapter type

- **What.** In `_apply_concert_plan`, allow the offline fingerprint / candidate-pool match to override a generic chapter label. Add a new `chapter.type ∈ {song, mc, intermission, no_song}` field; teach `_SETLIST_SKIP` to consume it. Auto-classify `Fuura`-shape titles (proper nouns not in the candidate pool and not in the library) as `no_song`.
- **Signal.** C4 chapters `La La La / Face It / Kton Boogie` get correctly identified via corroboration; `Fuura` becomes a fast reject; C1 concert-audio plan finally has something to override with.
- **Impact.** C1, C4 primarily; C3 (no chapters, but the corroboration threshold tuning generalizes).
- **Size.** Small. Existing state machine, new state field, new tune knobs `chapter_override_min_score / chapter_no_song_reject`.
- **Status.** ✅ **Landed in v1.1.64.** Tune knobs `chapter_override_min_score=0.70` (was hardcoded) + `chapter_no_song_reject=0` (opt-in). `_SETLIST_SKIP` widened with `greeting / closing / break / interval / freetalk / 挨拶 / 自己紹介 / オープニング / エンディング / アンコール / 休憩 / 幕間 / credits / staff roll / スタッフロール` (+ EN + JP variants of dance-break / instrumental-break). New early gate in `_concert_setlist_tick`: when `chapter_no_song_reject=1`, chapter titles matching neither the candidate pool (loose case-fold substring) nor the local library index are treated as non-song (Phase Connect "Fuura" case). Turn on for concerts that have a candidate pool but chapters that look like proper nouns / tributes.

### P3 — Whisper-ASR fallback song ID *(TICKET-063 open follow-up)*

- **What.** When OCR + Shazam + boundary + candidate-pool all miss, transcribe ~12 s of vocals via `faster-whisper small` and lyric-match against the whole library (or the candidate pool if we have one). Uses `align.transcribe_vocals` and `align.score_candidates` (already exist for `_decide_by_ear`).
- **Signal.** C1's Deep Dive premiere (brand-new song, no prior fingerprint) gets recognised via lyric text alone; C5's Endless, Nameless finale correctly identified through the noise-jam.
- **Impact.** C1 (brand-new songs), C5 (noise-jam), and generally the "no prior fingerprint" long-tail.
- **Size.** Medium. Faster-whisper is already bundled (non-lean build); wiring into `_decide_by_ear`'s scope-to-pool path is the main work.
- **Status.** *Not started.*

### Deferred / research

- **Native-title alias auto-expansion.** After `parse_song_candidates` returns, cross-match against `_NATIVE_TITLE_ALIASES_RAW`. When both a romaji spelling AND a native-script spelling appear on the same channel, learn the alias automatically. Persist to `settings.json`. Would fix `feelingradation` and `Kton Boogie → 混沌ブギ` in one shot.
- **Encore-gap heuristic.** For concerts > 45 min without an explicit encore chapter, the ~1-2 min gap before Idol / Endless-Nameless-style closer is nearly universal. Anchor a re-identify on gap-of-30 s+ after position > `0.75 * duration`.
- **Concert-specific Whisper model.** Fine-tuning on live audio (applause + reverb + arrangement drift) — research-only for now.
- **Setlist.fm live pull.** For `hasChapter=false && hasParsedSetlist=false` cases, consult setlist.fm via artist+date. Small script, real setlist.fm auth. Cheap once wired.
- **Paste-in `MM:SS – Title` setlist API endpoint.** Manual override for user's own recordings / uploads where they know the setlist. `POST /concert/plan` → JSON. Useful for members-only encore videos and personal recordings.
- **OCR banner: hardcoded-subtitle rejection.** Discriminate a stable top-left title card from mid-frame subtitle text (position + persistence heuristics). Helps the C3 Chinese-subtitle reupload case.

---

## 5. Auto-tune harness expansion for concerts

The existing harness lives at `D:\Lyric-Immersion-Auto` (branch `auto-tune`). This section proposes the additions.

### New corpus file: `auto/corpus/concerts.yaml`

Mirror the five entries above with the following per-track fields (v0 of a concert corpus schema):

```yaml
- key: rise_in_motion_hajime_2026
  url: https://www.youtube.com/watch?v=Zcx1344XAmI
  title: "【3DLIVE】Rise In Motion【轟はじめ】 #轟はじめ生誕祭2026"
  artist: "轟はじめ (Todoroki Hajime)"
  channel: "Hajime Ch. 轟はじめ ‐ ReGLOSS"
  duration_s: null              # unverified
  is_concert: true
  has_mc: true
  has_intermissions: true
  has_chapter_markers: false
  setlist_confidence: low
  vtuber: true
  language: ja
  weight: 3.0                   # birthday-live has candidate-pool value
  setlist: []                   # low-confidence — leave empty until fan setlist appears
  mc_segments_s: []             # e.g. [[125, 240], ...]  measured manually per run
  known_pending_releases:
    - "Deep Dive"               # released day after; brand-new-song test
  failure_modes:
    - wrong_song_generic
    - cross_lang_ko_zh
    - mc_drift
    - chorus_trap
    - brand_new_no_fingerprint
```

### Concert-specific scoring components

Additions to `auto/scorer.py`. Weights are provisional (calibrate on the first real run).

| Component | Weight | What it measures |
|---|---:|---|
| `per_song_correct_lock` | 40 | Fraction of setlist positions where the correct song locked within `sync_window_s = 20 s` of the golden start time. Needs `setlist[].start_s`. |
| `song_transition_latency_p95` | 15 | p95 seconds from a known song boundary until the next correct song locks. Encourages fast transitions. |
| `mc_drift_s_total` | 15 (penalty) | Sum of seconds we held stale lyrics through a known `mc_segments_s` interval. Lower better; 0 = perfect. |
| `wrong_song_strike_storms` | 10 (penalty) | Count of dead-segment windows where `_decision.strikes > threshold` (proxy: strikes rate > X per 30 s). |
| `cover_attribution_correct` | 10 | Fraction of cover songs where `meta.artist == golden.original_artist` (not the covering VTuber). |
| `chapter_override_correct` | 5 | For concerts with chapter markers: fraction where the label was correctly kept or correctly overridden by corroboration. |
| `finale_locked` | 5 | Binary — did the final song (encore) lock correctly. Isolated because encores have unique gap dynamics. |

Fitness = weighted mean across the corpus (weights per-concert via YAML `weight`). Cap catastrophic-regression at 60 aggregate points (any concert scoring < 20 caps the trial).

### New CLI subcommands

```pwsh
# Full 5-concert run:
python -m auto concerts --corpus concerts.yaml --window 900

# Duel two candidate-pool configs on the concert corpus:
python -m auto duel --a defaults --b pool-scoped --corpus concerts --window 300

# Score-only mode (post-hoc scoring against a saved app run):
python -m auto concerts --score-only --replay-metrics D:\DesktopKaraoke\metrics.json
```

### Ground-truth capture workflow

Filling in `setlist[].start_s` + `mc_segments_s` on the corpus is the tedious part. Proposed once-per-concert flow:

1. User watches the concert with the app running, presses the tray-menu "🔖 Mark song boundary (concert)" between songs. **Landed in v1.1.64** as `Overlay.mark_concert_boundary()` — appends `{ts, wall_iso, smtc_title, smtc_artist, player_pos_s, duration_s, offset_s, video_url, loaded_song_title, showing_idx, live_mode, live_arrangement, mv_mode, concert_candidates[]}` to `<data>/concert_marks.jsonl`. Tray item only visible while a concert wrapper is active.
2. After the concert, `python -m auto concerts --harvest-marks concert_marks.jsonl` merges the marks into the YAML *(harness command still pending — see §7 Q5)*.
3. MC intervals are the gaps between song ends and next song starts, clipped by user-marked start/end (or a heuristic: interval > 30 s = MC).

Alternative: `auto concerts --auto-mark <url>` runs the app once, extracts boundaries from `_on_boundary` events (heuristic, needs manual review).

---

## 6. AutoResearch integration plan

**What it is.** `uditgoenka/autoresearch` (5.2 k stars, MIT) is a **Claude Code skill** — a bundle of Markdown slash-commands + hook safety scripts installed into a Claude Code (or OpenCode / Codex) session. It generalises Karpathy's `modify → verify → keep or git-revert → repeat` autoresearch pattern to any domain with a mechanical numeric metric. Ships 14 slash-commands (`/autoresearch`, `/autoresearch:plan`, `/autoresearch:debug`, `/autoresearch:fix`, `/autoresearch:security`, `/autoresearch:ship`, `/autoresearch:scenario`, `/autoresearch:predict`, `/autoresearch:learn`, `/autoresearch:reason`, `/autoresearch:probe`, `/autoresearch:improve`, `/autoresearch:evals`, `/autoresearch:regression`) plus 9 safety hooks.

**What it is NOT.** Not a Python library. Not a web-research / fetch / search client. Not a standalone CLI outside Claude Code / OpenCode / Codex. It drives the *host* coding agent in a bounded loop using **git as memory** (commits with `experiment:` prefix, auto-revert on regression) and TSV as the results log.

**Install.**

```pwsh
# Inside a Claude Code session, from the repo root:
npx skills add uditgoenka/autoresearch
# RESTART Claude Code (documented limitation) — commands become visible after restart.
```

Or via plugin marketplace: `/plugin marketplace add uditgoenka/autoresearch` then `/plugin install autoresearch@autoresearch`.

Optional: `AR_NOTIFY_WEBHOOK` env var for a Slack notification on session end. `.ckignore` at repo root customises which directories the safety hooks block.

**Deps.** None of its own. Git (for the commit/revert loop and `git worktree` on regression). Whatever runtime the user's Verify/Guard commands need (`pytest`, `python -m auto ...`).

### Concrete integration patterns for this project

**Pattern A — Nightly auto-tune on concerts.**
```
/autoresearch
Goal: raise concerts.yaml aggregate score from baseline to ≥ 65
Scope: main.py:5141-5600 main.py:11050-11110 concert_ocr.py concert_audio.py yt_description.py
Guard: pytest tests/  &&  python -m auto smoke --corpus easy --window 15
Metric: aggregate concerts score
Verify: python -m auto concerts --corpus concerts.yaml --window 300 --print-score
Iterations: 50
```
Each iteration commits with `experiment:` prefix; regressions on the Guard command auto-revert. Runs overnight; results TSV lands in `.claude/autoresearch/`. Directly targets the wrong-song / candidate-pool wiring push.

**Pattern B — Setlist-discovery loop for corpus expansion.**
```
/autoresearch:probe --chain plan,autoresearch
Topic: produce a validated setlist JSON for <YouTube URL>
```
The probe command's 8 adversarial personas (Ambiguity Detective / Contradiction Finder / Success-Criteria Auditor / …) force the loop to reconcile chapter titles vs description order vs comment timestamps and refuse to emit a setlist until ≥ 2 sources agree per song. Verify checker: monotonic timestamps, no gaps > 3 min without an MC segment, title-normalisation against the alias table. Output: per-URL `setlist.json` committed alongside the concert media.

**Pattern C — Per-song ground truth enrichment.**
```
/autoresearch:improve --depth deep --icp "VTuber concert lyric-overlay users, JP fans of Kotoha/KOKO/Kamitsubaki/V.W.P"
```
For each row in a setlist: canonical native title, romaji, EN gloss, original artist, is-cover, language, NetEase / Genius URLs, aliases seen on YouTube. Populates `auto/corpus/ground_truth/<slug>.md` and a flat `corpus.tsv`. Directly closes the "fetch searches romaji not native title" gap.

**Pattern D — Failure-mode scenario generation.**
```
/autoresearch:probe --improve
Topic: live concert lyric identification and time-anchoring
```
then
```
/autoresearch:scenario --format test-scenarios --focus edge-cases --depth deep --domain product
```
Generates 50+ named failure scenarios (applause-covered onset, bilingual MC banter, medley-without-gap, cover-of-JP-song-sung-in-EN, chorus-repeat false-agree, katakana-vs-hiragana mismatch, TPVR shared-member cross-artist ambiguity). Save each as a corpus fixture with an expected assertion; nightly loop's metric then includes named-scenario pass rate. **Net effect: the auto-tune harness stops being "make the number go up" and becomes a growing, human-readable failure catalog.**

**Pattern E — Release-gate v1.1.64+ with a stability contract.**
```
/autoresearch:regression --predict --evals --fix --ship
```
Baseline = previously-released tag on the concerts corpus (via `git worktree`); candidate = HEAD. HARD dims: functional (song-lock correctness), api-contract (concert plan JSON schema), integration-e2e (offline `concert_audio.py` full run). SCORE dims: perf (time-to-first-lock p95), resource (VRAM peak on Whisper), visual-ui (overlay screenshot diff). Green→red on song-lock correctness blocks the tag; `--fix` cycles let the loop attempt a targeted patch before `--ship`.

**Caveats to plan around.**
- **Iteration cost.** 25-100 iterations per goal on the Claude account; concert Verify runs are ~5-10 min each. Budget 5-10 h of concurrent Claude Code time per full `--autoresearch` run.
- **Safety hooks.** 9 hooks block reading .env / SSH keys / node_modules; may need per-hook opt-out (`AR_DISABLE_SCOUT_BLOCK`, `AR_DISABLE_PRIVACY_BLOCK`) for a harness that legitimately reads model caches / D:\secrets\. **`D:\Lyric-Immersion-AR\.ckignore` is already seeded with our expected reads.**
- **`experiment:` commits.** Would pollute release history on `master`. Always run autoresearch on a dedicated worktree. ✅ **`D:\Lyric-Immersion-AR` worktree is set up on branch `autoresearch` (v1.1.64).** Unset upstream so a bare `git push` refuses. See `D:\Lyric-Immersion-AR\AR_README.md`.
- **Not a fetcher.** For Pattern B / C, the actual data collection is done by the host Claude Code's MCP tools (webclaw, WebFetch, gh). Autoresearch orchestrates but doesn't hit any network on its own.

---

## 7. Open questions

1. **When is candidate-pool wiring landing?** P0 is blocked on your explicit go per HANDOVER §"Known-open / pending" #1. Everything downstream depends on this.
YOU DECIDE WHATS BEST 

2. **Corpus location.** Add `auto/corpus/concerts.yaml` to `auto-tune` branch, or to `master` so the app can consume it directly (e.g. for `_load_concert_setlist` hard-coded fallbacks)?
MASTER


3. **MC-segment ground truth.** Is a tray-menu "mark boundary" hotkey acceptable UX for capturing per-run manual anchors? Alternative: log playback marks to a session file the user annotates after.
SURE TRAY MENU OK

4. **AutoResearch host worktree.** Would you rather I add a `D:\Lyric-Immersion-AR` worktree specifically for autoresearch loops, or run them inside `D:\Lyric-Immersion-Auto`? Downstream: the safety hooks' per-hook opt-out list.
YES


5. **Real audio.** The auto-tune harness is mock-mode only today. Concert scoring genuinely needs the offline `concert_audio.py` pipeline to run against real downloaded audio. Do we build a `--real-audio` mode next, or continue mock-only + hand-scoring for now?
YES AND DOWNLOAD THE VIDEOS AND ANALYZE LOCALLY AND ANAYLZE THE WHOLE WEBPAGE PER EACH FOR MORE WAYS WE CAN RECOGNIZE SONGS AND SYNC ON OTHER CONCERTS WE DONT HAVE THE LUXURY OF DOING THIS FOR

6. **C1 members-only encore.** [`coev0bBUn-c`](https://www.youtube.com/watch?v=coev0bBUn-c) exists but is members-locked. If you have access, does it change what songs we expect?
NOT NECESSARILY BUT I DONT HAVE ACCESS. WE ARE SUPPOSED TO WORK ON ANY SONG AND CONCERT VIDEO ALGORITHICALLY

7. **Nirvana as-outlier.** Keep it in the corpus for generalisation coverage, or split into a separate `western-rock.yaml` so VTuber-focused tuning doesn't accidentally over-fit to Kurt banter cadence?
UP TO YOU

---

## 8. Change log

| Date | Version | Change |
|---|---|---|
| 2026-07-04 | Doc v0.1 | Initial recon + 5-concert corpus + feature roadmap + AutoResearch integration plan. Baseline app version 1.1.63. |
| 2026-07-04 | Doc v0.2 | User answered §7 open questions. Shipped in **v1.1.64**: P0 candidate-pool wiring (`concert_pool_scoped=1`, `concert_pool_prefetch_max=20`, `_prefetch_concert_candidates`, `_decide_by_ear` scoped-pool inclusion + whole-library-expansion skip when pool wins), P2 chapter-corroboration knob (`chapter_override_min_score=0.70`, tunable) + `chapter_no_song_reject=0` (opt-in) + widened `_SETLIST_SKIP`, and tray "🔖 Mark song boundary (concert)" + `Overlay.mark_concert_boundary()` writing `<data>/concert_marks.jsonl`. Machine-readable [`docs/concerts.yaml`](concerts.yaml) corpus checked in. AutoResearch worktree at `D:\Lyric-Immersion-AR` (branch `autoresearch`) + `.ckignore` + `AR_README.md`. **Still pending:** P1 MC gate; P3 Whisper fallback; auto-tune harness `concerts` subcommand + real-audio mode. |
