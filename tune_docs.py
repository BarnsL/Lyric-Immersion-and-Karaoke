"""Per-knob documentation for the ~230 live-tunable parameters (TICKET-212).

WHY THIS IS A SEPARATE MODULE, AND WHY IT IS NOT IN main.py's tune dict
----------------------------------------------------------------------
The dev console lets you change any of these at runtime, and until now each was
presented as a bare key name and a value. `deadband` and `energy_lift_floor` are
not self-explanatory; adjusting them was guesswork, and a guess that makes the
overlay worse is hard to attribute later.

The obvious home would be an inline comment on each entry of `self._tune` in
main.py, and 178 of them do have one. Those comments cannot be shipped: the
frozen PyInstaller build has no source to parse, so a runtime reader would find
nothing. Hence an explicit module, imported by main.py and bundled by the spec.

CONTRACT
--------
Exactly one entry per key in `Overlay._tune`, no more and no fewer. That is not
a convention, it is enforced: `scripts/probe_tune_docs.py` fails when a knob is
added without a doc or a doc outlives its knob. Add a knob, add a line here.

Each entry states, in order: what the knob controls, what raising it does, what
lowering it does, a sensible range with the default, and which subsystem reads
it. ASCII only, so it renders identically in a tooltip, a terminal and a log.

Three knobs are documented as having NO reader (`continuous_recal_ms`,
`live_resync_s`, `yt_description_cache_days`). That is deliberate and accurate:
they are registered in the tune dict but nothing consumes them, and saying so is
more useful than inventing an effect for a knob that does nothing.
"""

TUNE_DOC = {
    "jank_backoff_unverified_cap_s":
        "Caps how long identification stays paused after a stutter, for songs "
        "whose lyrics are not yet confirmed. Raise it to protect frame rate "
        "harder during heavy scenes, at the cost of taking longer to notice the "
        "wrong song is loaded. Lower it so an unverified song keeps being "
        "checked even while frames are being missed. Typical 6 to 30, default "
        "12. Read by the identify jank backoff in main.py. Note this knob was "
        "read by the code but missing from the tunable table until TICKET-211, "
        "so setting it had no effect before that.",
    "sim_missing_whisper":
        "Pretends the faster-whisper library is not installed, so the "
        "no-AI-add-on code paths can be tested without uninstalling it. Set 1 "
        "to simulate absence, 0 for normal operation. With it on, generate by "
        "ear, sync by listening and force sync all show their needs-faster-"
        "whisper hints and decline. Default 0. Read by the feature gate helper "
        "in main.py. The build selftest and the diagnostics page deliberately "
        "ignore this and keep reporting what is really installed.",
    "sim_missing_model":
        "Pretends the whisper model weights are absent, so the first run "
        "download prompt and its longer watchdog can be exercised without "
        "deleting roughly 2 GB. Set 1 to simulate absence, 0 for normal "
        "operation. With it on, generation reports that it is fetching the "
        "voice model and uses the longer stall timeout. Default 0. Read by the "
        "feature gate helper in main.py. Simulates a missing model, not the "
        "real download, so no bytes are actually fetched.",
    "sim_missing_gpu":
        "Pretends the CUDA and GPU libraries are absent, so the CPU only "
        "fallback can be tested on a machine that has a working GPU. Set 1 to "
        "simulate absence, 0 for normal operation. With it on, the components "
        "view reports the GPU as effectively unavailable. Default 0. Read by "
        "the feature gate helper in main.py. Note it does not currently force "
        "whisper itself onto the CPU, which is chosen inside the align module.",
    "sim_missing_ytdlp":
        "Pretends yt-dlp is not installed, so the paths that need it can be "
        "tested with it still present. Set 1 to simulate absence, 0 for normal "
        "operation. With it on, deep generation quietly does nothing and the "
        "caption fetch shows its needs-yt-dlp hint, which is what a fresh "
        "install without the add-on sees. Default 0. Read by the feature gate "
        "helper in main.py.",
    "agree":
        "How close, in seconds, two consecutive listen results must land before the "
        "app believes them and commits the offset on a normal studio track. Raise "
        "it and reads agree more easily, so sync locks faster but a repeated chorus "
        "can fool it. Lower it and only genuinely matching reads commit, which is "
        "safer but slower. Typical 1.0 to 3.0, default 2.0. Read by the studio "
        "branch of the Shazam sync reader.",
    "agree_live":
        "The same two read agreement window as agree, but used for live and concert "
        "arrangements, where tempo wanders and reads scatter more. Raise it to "
        "accept looser pairs so a drifting live track still locks. Lower it to "
        "demand closer agreement, which reduces false locks onto a repeated chorus "
        "but can leave a concert unsynced. Typical 2.0 to 6.0, default 4.0. Read by "
        "the live follow branch of the Shazam sync reader.",
    "applause_min_s":
        "How many continuous seconds of loud, untuneful crowd noise must build up "
        "before the app calls it an applause gap and treats it as a boundary "
        "between songs in a live set. Raise it so a burst of cheering in the middle "
        "of a song does not trigger a search for the next track. Lower it to catch "
        "back-to-back songs sooner, at the risk of firing mid-song. Typical 1 to 8, "
        "default 2.5. Read by the concert applause watcher in the main tick.",
    "assert_same_tick":
        "A developer check that warns when the playback offset is written more than "
        "twice in a single frame, which is a sign of an ordering bug. Set 1 to "
        "enable and have those cases written to the log. Set 0 to disable and stay "
        "silent, which is the normal shipping state. Default 0. Read by the offset "
        "write path in main.py.",
    "auto_align_cooldown":
        "The minimum seconds between two automatic listen and realign passes, which "
        "bounds how much CPU and audio capture the sync engine may spend. Raise it "
        "to save CPU and reduce recognition churn, at the cost of slower recovery. "
        "Lower it to re-lock more aggressively. Keep it below the fast tier "
        "interval or it throttles the cadence it supports. Typical 5.0 to 20.0, "
        "default 8.0. Read by align by listening.",
    "auto_align_min_pos":
        "How far into the song, in seconds of playback position, the app must be "
        "before an automatic realign may run. Very early positions have too little "
        "audio history to judge. Raise it to leave the opening of a track alone and "
        "avoid early false corrections. Lower it to start correcting sooner after a "
        "song begins. Typical 5.0 to 30.0, default 12.0. Read by the sync tier "
        "listen path and the fine tune listen tick.",
    "auto_game_mode":
        "Whether the overlay automatically switches to its Gaming look when a full "
        "screen game takes focus, then restores your previous look afterwards. Set "
        "1 to enable, the default, so lyrics move out of the way without you "
        "touching anything. Set 0 to disable and always keep the layout you chose. "
        "Default 1. Read at startup in main.py as the default for the tray toggle.",
    "auto_game_mode_arm_s":
        "How long a full screen game must hold focus, in seconds, before the "
        "overlay swaps to its Gaming look. Raise it to avoid switching during a "
        "brief glance at a game window. Lower it so the swap happens almost "
        "immediately when you enter a game. Typical 0.5 to 5, default 1.5. Read by "
        "the automatic game focus check in main.py when auto_game_mode is on.",
    "auto_game_mode_release_s":
        "How long the game must be gone from focus, in seconds, before your "
        "previous overlay look is restored. Raise it to stop the layout flapping "
        "back and forth when you switch in and out repeatedly. Lower it to get your "
        "normal layout back sooner after leaving the game. Typical 1 to 15, default "
        "4.0. Read by the automatic game focus check in main.py.",
    "belt_reseed_s":
        "In scrolling layouts, a jump in playback position larger than this many "
        "seconds is treated as a seek or track change, so the scroll belt re-seeds "
        "its baseline instead of sliding the whole column across the screen in one "
        "frame. Raise it to absorb bigger jumps as motion. Lower it to re-seed more "
        "readily and avoid lurches. Typical 0.2 to 2.0, default 0.5. Read by the "
        "horizontal and vertical scroll updates.",
    "caption_retime":
        "Whether the app re-times a fetched or generated lyric body onto the timing "
        "grid of the video's own automatic captions, which are locked to the video "
        "rather than to a recording. Set 1 to enable, which is the main sync anchor "
        "for browser music videos, or 0 to disable and rely on audio alignment "
        "only. Default 1. Read by the lyric load paths before scheduling a caption "
        "re-time.",
    "caption_retime_delay_s":
        "How long the app waits after a lyric body loads before it fetches caption "
        "timing in the background and re-times to it. Raise it to let the rest of "
        "the load settle first, which reduces stutter at song start but delays the "
        "anchor. Lower it to re-time sooner, closer to the start of the track. "
        "Typical 0.5 to 5.0, default 1.5. Read by the lyric load paths when "
        "scheduling the caption re-time.",
    "caption_retime_skip_off_s":
        "If a recent audio based sync is already locked with an offset smaller than "
        "this many seconds, the caption re-time is skipped so a very tight audio "
        "lock is not loosened onto the coarser caption grid. Raise it and more "
        "audio locks are protected, so captions are used less. Lower it and "
        "captions take over more often. Typical 0.1 to 1.0, default 0.4. Read by "
        "the apply caption re-time step.",
    "chapter_no_song_reject":
        "Set 1 to enable, 0 to disable. With 1, and only when a concert song list "
        "has been parsed, a chapter whose title matches neither that list nor "
        "anything in your library is treated as a talk or interlude segment and "
        "skipped rather than searched for. With 0 the app tries to fetch it anyway. "
        "Enable it to stop names of people or tributes thrashing the fetcher; leave "
        "it off so a lesser known original still gets a chance. Default 0.",
    "chapter_override_min_score":
        "How confident an audio fingerprint of a concert segment must be, from 0 to "
        "1, before it is allowed to replace the title written in the video's own "
        "chapter list. Raise it to protect chapter names a human typed against a "
        "stray mis-identification. Lower it to let a medium confidence fingerprint "
        "correct a vague chapter label. The offline pass scores 0.85 when two "
        "probes agree and 0.60 for a lone hit. Typical 0.5 to 0.95, default 0.70.",
    "concert_audio_floor_frac":
        "The loudness line that separates singing from applause in the offline "
        "concert pass, given as a fraction of the concert's own loud level rather "
        "than an absolute value. Raise it so only the loudest singing counts, which "
        "pushes each song's start later and can miss quiet numbers. Lower it and "
        "crowd noise starts counting as singing, so lyrics begin during the "
        "cheering. Typical 0.2 to 0.7, default 0.40. Read by the concert audio "
        "analyser.",
    "concert_audio_id_slice_s":
        "How many seconds of audio are fingerprinted per concert segment when "
        "working out which song it is. Raise it to give the fingerprint more to "
        "work with, which improves the chance of a match but makes the offline pass "
        "slower. Lower it for a quicker pass with weaker matches. Two probes are "
        "taken per segment, one at the start of the singing and one around 25 "
        "seconds later. Typical 6 to 30, default 12. Read by the concert audio "
        "analyser.",
    "concert_audio_identify":
        "Set 1 to enable, 0 to disable. With 1 the offline concert pass "
        "fingerprints a slice of each segment, so a song can be named even when the "
        "chapter label is vague or the video has no chapters at all. With 0 the "
        "pass only refines where the singing starts and makes no network requests. "
        "Default 1. Passed into the concert audio analyser as its identify flag, "
        "from main.py.",
    "concert_audio_max_dur_s":
        "The longest video, in seconds, the offline concert pass will download and "
        "analyse. Anything longer is assumed to be a compilation or archive rather "
        "than one concert, and is skipped. Raise it so very long uploads are still "
        "analysed, at the cost of a large download and a long wait. Lower it to "
        "bail out sooner and save bandwidth, at the risk of skipping a genuinely "
        "long concert. Typical 1800 to 14400, default 4800, which is 80 minutes.",
    "concert_audio_min_song_s":
        "The shortest run of sustained singing the offline pass will accept as a "
        "song when the video has no chapters to go by. Raise it so short numbers "
        "and interludes are merged or ignored, giving fewer false segments but "
        "possibly losing a brief song. Lower it to pick up short songs, at the risk "
        "of turning applause breaks and talking into segments of their own. Typical "
        "20 to 120, default 45. Read by the concert audio analyser.",
    "concert_audio_on":
        "Master switch for the offline concert pass, which downloads the concert "
        "audio once, works out where the singing really starts in each song and "
        "fingerprints it. Set 1 to enable, 0 to disable. With 1 you get accurate "
        "per-song anchors and corrected titles with no stutter, since it runs in "
        "the background. With 0 the app relies on chapters, screen reading and live "
        "listening alone. Default 1. Read before the background analysis thread "
        "starts.",
    "concert_first_read_max_s":
        "The knob that actually speeds up concerts. On the live listening path, a "
        "first correction smaller than this many seconds is applied straight away "
        "instead of waiting for a second agreeing reading, because a concert can "
        "change song before that second reading arrives. Raise it and larger "
        "corrections commit on the first reading. Lower it, or set 0, and "
        "everything waits to be paired. Typical 0 to 4, default 1.8. Read by the "
        "live sync-follow branch.",
    "concert_pool_prefetch_max":
        "How many songs from a concert's parsed song list get their lyrics "
        "downloaded in the background as soon as the concert loads. Raise it so "
        "more of the set is ready before it is performed, at the cost of more "
        "network requests up front. Lower it to be gentler on the network and start "
        "faster, leaving later songs to fetch when they arrive. Typical 5 to 60, "
        "default 20. Read by the concert candidate prefetcher in main.py.",
    "concert_pool_scoped":
        "Set 1 to enable, 0 to disable. With 1, song identification inside a "
        "concert is narrowed to the song list parsed out of the video description: "
        "those songs get their lyrics fetched in advance, they are scored first "
        "when the app decides by ear, and the whole-library search is skipped when "
        "they already match. With 0 the app matches blindly against the entire "
        "library. Default 1. Read by the candidate prefetcher and the decide-by-ear "
        "scorer.",
    "concert_setlist_on":
        "Set 1 to enable, 0 to disable. With 1 the app pulls the live video's "
        "chapter list and uses those per-song titles and start times as the "
        "authoritative running order, which is the most reliable way to know what "
        "is being performed. With 0 that source is ignored and the app falls back "
        "on reading the screen and identifying by ear. Default 1. Read at the live "
        "video branch and the concert detection flip in main.py.",
    "concert_single_shot_max_s":
        "The same single-reading correction ceiling as the live-arrangement one, "
        "but for full concerts, in seconds. Raise it and bigger concert corrections "
        "commit off one reading, which is quicker but less checked. Lower it and "
        "more corrections wait for a confirming reading. Note that the sync tier "
        "bows out early during a real concert, so in practice this mostly affects a "
        "live arrangement that reached the tier path. Typical 0 to 4, default 1.8.",
    "concert_tpvr_gap_s":
        "In concert mode, how many seconds pass between a held timing reading and "
        "the confirming second reading on the sync tier path. Raise it to separate "
        "the two readings further in the music, which guards against a repeated "
        "chorus, at the cost of a slower lock. Lower it to confirm faster. As with "
        "the concert single-shot ceiling, the tier path bows out early in a real "
        "concert, so this rarely applies. Typical 0.5 to 4, default 1.2.",
    "confirmed_recal_s":
        "Once a song is confirmed and being watched by the song change detector, "
        "fingerprint re-locks are spaced at least this many seconds apart, because "
        "the adaptive sync tier already handles drift. Raise it to cut recognition "
        "work and CPU on long stable tracks. Lower it to re-verify identity more "
        "often. Typical 20 to 90, default 45.0. Read by the recalibration scheduler "
        "when choosing the next poll delay.",
    "continuous_recal_ms":
        "A legacy fixed cadence, in milliseconds, for background sync re-checking, "
        "kept for reference from before the adaptive verification tier replaced it. "
        "Raising or lowering it has no effect on current behaviour, because nothing "
        "in the running code reads it. Default 15000. Registered only in the "
        "tunable table in main.py near line 3295, and described in docs/ISSUES.md "
        "and the energy correlation notes.",
    "corroborated_lock_immunity":
        "Whether a lyric body that is both exact title locked and corroborated "
        "against the audio becomes immune to being torn down and replaced, so a "
        "song that has proven it is correct is re-synced rather than blacklisted "
        "when it drifts. Set 1 to enable this protection, or 0 to leave such bodies "
        "re-checkable. Default 1. Read by the decision engine authority check in "
        "main.py.",
    "cover_amp_album_demote":
        "Handles titles that join two names with an ampersand, which can look like "
        "a cover. When the music source also reports an album, the track is almost "
        "certainly an official original, so this cancels the weak cover guess. Set "
        "it to 1.0 or above to enable the demotion, below 0.5 to disable it for "
        "diagnosis. Explicit cover tags are never demoted. Default 1.0. Read by the "
        "track metadata parser.",
    "cpu_dedicate_last_core":
        "How the app shares the processor. Set 1 to enable, pinning it to the last "
        "physical core at raised priority, which keeps the overlay perfectly smooth "
        "while a game uses the remaining cores. Set 0 to disable, spreading it over "
        "upper cores at lowered priority, which suits a machine with no graphics "
        "card doing heavy lyric generation. Default 1. Read by the priority setup "
        "in main.py.",
    "deadband":
        "The size of a timing error, in seconds, that the app treats as close "
        "enough to ignore. Raise it and small corrections stop nudging the lyrics "
        "on an already stable song, at the cost of letting real drift sit longer. "
        "Lower it to chase tighter sync, at the cost of more visible movement. "
        "Typical 0.4 to 1.2, default 0.8. Read by the Shazam sync reader, the live "
        "follow path, and the screen reader sync gate in main.py.",
    "decide_after_verified":
        "Whether by-ear checking continues once a song is fully confirmed, meaning "
        "verified, title-locked and word-checked. Set 0 to stop checking and trust "
        "that result, so a noisy transcript cannot override known-good lyrics. Set "
        "1 to keep checking forever, extra paranoia at the cost of CPU and switch "
        "risk. Default 0. Read by the decide-by-ear entry gate.",
    "decide_at_s":
        "How far into a new track the first by-ear check runs, giving the song time "
        "to reach actual singing. Raise it to wait out long instrumental intros so "
        "the listen is not wasted on silence. Lower it to catch wrong lyrics "
        "sooner, at the risk of transcribing an intro. Typical 8 to 25 seconds, "
        "default 12.0. Read by the track-load scheduler, decide-by-ear and the "
        "late-load probe.",
    "decide_library_min":
        "The higher bar a candidate must clear when the search has widened from "
        "title-similar songs to the WHOLE cached library. Raise it so a short or "
        "noisy transcript cannot latch onto a stray song among hundreds. Lower it "
        "to let a broad search act on a weaker match. Keep it at or above "
        "decide_min_score. Typical 50 to 75, default 60.0. Read by decide-by-ear.",
    "decide_listen_s":
        "How many seconds of live vocals are recorded and transcribed for one by- "
        "ear decision. Raise it for a longer, more reliable sample that is likelier "
        "to catch real words, at the cost of more time and CPU per check. Lower it "
        "for a quicker, cheaper check that risks an inconclusive short transcript. "
        "Typical 8 to 20 seconds, default 12.0. Read by decide-by-ear and its retry "
        "timer.",
    "decide_margin":
        "How many points better than the loaded song a rival candidate must score "
        "before the app will switch to it. Raise it to require a decisive win and "
        "hold the current lyrics through close calls. Lower it to switch on slimmer "
        "evidence, at the risk of flipping between two similar songs. Typical 8 to "
        "25, default 12.0. Read by decide-by-ear when comparing ranked candidates.",
    "decide_min_score":
        "The match score, out of 100, that a candidate song's lyrics must reach "
        "against the transcribed singing before that candidate can win a by-ear "
        "decision. It is also the bar at which the loaded body counts as confirmed "
        "by the words heard. Raise it to demand a clearer match and switch less "
        "often. Lower it to act on weaker evidence. Typical 45 to 70, default 55.0. "
        "Read by decide-by-ear.",
    "decide_probe_late_load":
        "Whether a lyric body that arrives AFTER the normal start-of-track check "
        "still gets word-checked, which covers slow provider fetches and generated "
        "lyrics. Set 1 to run a fresh by-ear probe on such late arrivals so a wrong "
        "body cannot ride out the whole song. Set 0 to check only at track start. "
        "Default 1. Read by the after-load hook.",
    "decide_probe_load_delay_s":
        "After lyrics arrive late in a track, how long to wait before probing them "
        "by ear, so the recording captures singing that belongs to the newly loaded "
        "body. Raise it to gather more settled audio before judging. Lower it to "
        "catch a wrong late body sooner. Typical 3 to 15 seconds, default 6.0. Read "
        "by the after-load hook that schedules the late probe.",
    "decide_titlelock_bump":
        "Extra points added to the score a candidate must reach when the loaded "
        "song was locked in by a confident title match, so listening must be "
        "clearly better before it may override the title. Raise it to defend title- "
        "matched songs harder against noisy transcripts. Lower it to let listening "
        "win more easily. Typical 0 to 30, default 15.0. Read by decide-by-ear; "
        "skipped when the body is a stub.",
    "decide_titlelock_margin":
        "The minimum winning gap enforced when the loaded song was locked in by a "
        "confident title match, replacing the ordinary margin whenever it is the "
        "larger of the two. Raise it to make overriding a title-locked song harder "
        "still. Lower it, toward decide_margin, to let by-ear corrections through. "
        "Typical 15 to 40, default 28.0. Read by decide-by-ear; skipped for a stub "
        "body.",
    "decide_wrong_floor":
        "The score below which the loaded lyrics are judged not to match the "
        "singing at all, which widens the search to the whole cached library and, "
        "on a second consecutive failure, blacklists and re-fetches the body. Raise "
        "it to declare lyrics wrong more readily. Lower it to tolerate poor scores "
        "from noisy transcripts. Typical 20 to 45, default 32.0. Read by decide-by- "
        "ear.",
    "decision_action_cooldown_s":
        "Minimum seconds between two automatic switch or regenerate actions, and "
        "also the length of the hold applied after a drift-only alarm is "
        "suppressed. Raise it to stop the app repeatedly tearing down lyrics on a "
        "difficult song. Lower it to allow quicker successive corrections. Typical "
        "15 to 90 seconds, default 30.0. Read by the decision engine action "
        "handler.",
    "decision_caution_strikes":
        "How many accumulated strikes move the watchdog from trusting the lyrics to "
        "a cautious state, where it nudges for verification but changes nothing on "
        "screen. Raise it to stay relaxed longer. Lower it to grow suspicious "
        "sooner. Covers lower this bar by one automatically. Typical 2 to 6, "
        "default 3. Read by the decision engine state machine and the status API.",
    "decision_engine_on":
        "Master switch for the background watchdog that scores source agreement, "
        "sync stability, lyric quality and by-ear corroboration every few seconds, "
        "then escalates from trust to caution to switch to regenerate. Set 1 to "
        "enable automatic wrong-lyric detection, 0 to disable it so only explicit "
        "checks and your own corrections act. Default 1. Read by the decision "
        "engine tick.",
    "decision_regen_strikes":
        "How many accumulated strikes push the watchdog past re-fetching and into "
        "generating lyrics from the audio itself, the last resort when no provider "
        "seems to have the right words. Raise it to give ordinary lyric sources "
        "more chances first. Lower it to reach generation sooner on obscure songs. "
        "Keep it above the switch level. Typical 5 to 12, default 8. Read by the "
        "decision engine.",
    "decision_score_window":
        "How many recent verdicts the watchdog keeps in its rolling history for "
        "each of its four checks. This history is purely diagnostic: it feeds the "
        "developer console and the status API, which shows the last six. Raise it "
        "to keep a longer trail for troubleshooting, lower it to keep less. Strike "
        "counting is unaffected. Typical 6 to 30, default 12. Read when the "
        "engine's history buffers are built.",
    "decision_switch_strikes":
        "How many accumulated strikes make the watchdog decide the lyrics are wrong "
        "and replace them, by blacklisting the current body and re-fetching. Raise "
        "it to hold the current lyrics longer and avoid tearing down a song that "
        "has merely drifted. Lower it to catch a genuinely wrong song sooner. Keep "
        "it above the caution level. Typical 3 to 8, default 4. Read by the "
        "decision engine.",
    "decision_tick_interval_s":
        "How often the background watchdog re-scores the loaded lyrics. Raise it to "
        "check less often, cutting CPU but slowing every automatic reaction, since "
        "strikes can only accumulate once per tick. Lower it for faster detection "
        "at higher cost and a twitchier engine. Typical 1 to 5 seconds, default "
        "2.0. Read by the decision engine tick, which self-throttles in the frame "
        "loop.",
    "discord_rpc_on":
        "Whether Discord Rich Presence is read as a last resort source of the "
        "current song title and artist. Set 1 to enable, which helps when audio "
        "plays on a device that reports nothing to Windows, such as a phone feeding "
        "a speaker. Set 0 to disable, the default, and never contact Discord at "
        "all. Default 0. Read by the music source fallback chain in main.py.",
    "discord_rpc_poll_s":
        "The minimum wait in seconds between requests asking Discord what is "
        "playing. Raise it to bother Discord less often and use slightly less "
        "power, at the cost of noticing a song change later. Lower it to react "
        "faster, though Discord itself refreshes only every few seconds so very "
        "small values gain nothing. Typical 3 to 30, default 5.0. Read by the "
        "Discord fallback path in main.py.",
    "discord_rpc_silent_gap_s":
        "How many seconds both the Windows media info and the live listening source "
        "must say nothing before Discord is allowed to supply a song. Raise it so "
        "Discord speaks only after a long confirmed silence, keeping it from "
        "overriding a real player. Lower it to fall back sooner. Typical 3 to 30, "
        "default 8.0. Read by the music source fallback chain in main.py.",
    "discord_rpc_timeout_s":
        "The hard limit in seconds on a single exchange with Discord, so a hung "
        "connection can never freeze the display. Raise it to give a slow or busy "
        "Discord more chance to answer. Lower it to guarantee the app never waits "
        "long, at the cost of giving up on some replies. Typical 0.2 to 2.0, "
        "default 0.5. Read by the Discord fallback path in main.py.",
    "display_lead_s":
        "How many seconds ahead of the true playback position the lyrics are drawn, "
        "so the highlight arrives just before the vocal. Raise it and the highlight "
        "runs earlier, which masks a systematic lag but eventually looks premature. "
        "Lower it, or set 0, and the highlight sits exactly on the measured clock. "
        "Typical 0.0 to 0.15, default 0.0. Read by the overlay position calculation "
        "each frame.",
    "display_lost_grace_s":
        "How long the overlay keeps waiting on a chosen monitor that has vanished, "
        "for example during a GPU driver reload, before it gives up and falls back "
        "to another screen. Raise it to ride out longer blackouts without the "
        "lyrics moving. Lower it to relocate faster. Typical 3 to 30 seconds, "
        "default 10.0. Only consulted when display_stick_to_selected is 0; read by "
        "the display picker in main.py.",
    "display_stick_to_selected":
        "Controls whether the overlay refuses to leave the monitor you picked. Set "
        "1 to enable, so the lyrics stay put through a screen blink, GPU reload or "
        "app hang and snap back when the display returns. Set 0 to disable, "
        "restoring the older behaviour that waits display_lost_grace_s then drifts "
        "to the closest or primary screen. Default 1. Read by the display picker in "
        "main.py.",
    "drift_align_trigger":
        "The app multiplies how wrong the sync is by how long it has stayed wrong, "
        "and once that running total passes this number it forces a fresh listen "
        "and realign. Raise it to be more patient, so brief wobble does not trigger "
        "extra recognition work. Lower it to react to sustained drift sooner. "
        "Typical 3.0 to 12.0, default 6.0. Read by the drift accumulator in the "
        "Shazam sync reader.",
    "drift_min_for_accum":
        "Only timing errors larger than this many seconds count toward the running "
        "drift total described above, so ordinary jitter never accumulates. Raise "
        "it and only clearly wrong sync feeds the accumulator, which means fewer "
        "forced realigns. Lower it and smaller persistent errors start counting, so "
        "realigns fire more readily. Typical 0.3 to 1.5, default 0.8. Read by the "
        "drift accumulator in the Shazam sync reader.",
    "drift_monotonic_reads_n":
        "How many consecutive sync readings must all err in the same direction "
        "before the app calls the drift steady and one directional rather than "
        "random noise, which then unlocks a faster recovery path. Raise it to "
        "require a longer clear trend, reacting later but more confidently. Lower "
        "it to spot creeping drift sooner. Typical 2 to 6, default 3. Read by the "
        "drift sign tracker in the sync reader.",
    "drift_recovery_cooldown":
        "The shorter realign cooldown used once steady one directional drift has "
        "been detected, replacing the normal automatic realign cooldown so a "
        "creeping song re-locks sooner. Raise it to hold back and spend less CPU "
        "during recovery. Lower it to hammer the correlator until the creep is "
        "caught. Typical 2.0 to 12.0, default 5.0. Read by align by listening when "
        "choosing the cooldown.",
    "ear_short_loaded_max":
        "When only a few words were heard, the highest score the CURRENTLY loaded "
        "lyrics may have for that short listen still to count as decisive. A brief "
        "read is only trustworthy when the loaded body has essentially no support. "
        "Raise it to allow action against better-scoring bodies. Lower it to be "
        "stricter. Typical 0 to 20, default 8.0. Read by decide-by-ear.",
    "ear_short_margin":
        "When only a few words were heard, how far ahead the winning candidate must "
        "be of the loaded lyrics before that short listen may act. Raise it to "
        "demand an overwhelming gap and so ignore more short reads. Lower it to act "
        "on narrower evidence from brief transcripts. Typical 30 to 60, default "
        "45.0. Read by the short-transcript check inside decide-by-ear.",
    "ear_short_switch_min":
        "When only a few words were heard, the score a rival candidate must reach "
        "before that short listen is allowed to act at all. Raise it to require a "
        "strong match before trusting a brief transcript. Lower it to let weaker "
        "short listens count, at the risk of acting on a deceptive near-tie. "
        "Typical 45 to 75, default 55.0. Read by the short-transcript check inside "
        "decide-by-ear.",
    "ear_thin_body_lines":
        "The line count below which the loaded lyrics count as a near-empty stub "
        "which, together with a near-zero match score, lifts the guards that "
        "normally block a by-ear switch. Raise it to treat larger bodies as "
        "disposable and rescue more readily. Lower it to protect even very short "
        "bodies from being replaced. Typical 3 to 15 lines, default 8. Read by "
        "decide-by-ear.",
    "ear_thin_body_score":
        "The match score at or below which the loaded lyrics count as unsupported "
        "by the singing, the second half of the stub test alongside the line count. "
        "Raise it to call more bodies worthless and so allow rescue switches more "
        "often. Lower it to require the loaded body to be almost totally unmatched "
        "first. Typical 0 to 20, default 8.0. Read by decide-by-ear.",
    "ease_deadzone_s":
        "When the displayed timing is within this many seconds of its target, the "
        "app snaps the rest of the way instead of continuing to ease, because the "
        "remaining difference is below what anyone can see. Raise it to end glides "
        "earlier and save per frame work, at the cost of a slightly coarser "
        "landing. Lower it for a finer approach. Typical 0.01 to 0.15, default "
        "0.05. Read by the eased offset routine.",
    "ease_max_step_frac":
        "The largest share of the remaining timing gap that a single rendered frame "
        "may close, expressed as a fraction of 1. It stops one slow frame from "
        "swallowing most of a correction and producing a visible snap. Raise it "
        "toward 1 for faster catch up with more risk of a jolt after a stall. Lower "
        "it for a guaranteed multi frame ramp. Typical 0.10 to 0.50, default 0.20. "
        "Read by the eased offset routine.",
    "ease_pull_per_sec":
        "How strongly the displayed timing is pulled toward its target in line "
        "mode. The glide is exponential, so a higher number closes most of the "
        "remaining gap sooner. Raise it for a snappier catch up that reads as a "
        "quick snap. Lower it for a slower, softer approach that can feel sluggish. "
        "Typical 1.0 to 6.0, default 3.5. Read by the eased offset routine that "
        "smooths every applied correction.",
    "ease_pull_per_sec_scroll":
        "The exponential pull strength used in scroll through belt layouts, "
        "deliberately gentler than the line mode value because belt motion makes "
        "any sudden correction obvious. Raise it to close a re-anchor sooner, which "
        "looks more abrupt. Lower it for a softer slide that takes longer to "
        "finish. Typical 0.5 to 3.0, default 1.5. Read by the eased offset routine "
        "when a scroll layout is active.",
    "ease_slew_cap_s":
        "The fastest the displayed timing may glide toward a new corrected value in "
        "line mode, measured in seconds of correction per second of real time. "
        "Raise it and corrections land quickly but the movement is more visible. "
        "Lower it and the glide is gentler but a correction takes longer to fully "
        "arrive. Typical 1.0 to 6.0, default 3.0. Read by the eased offset routine "
        "that smooths every applied correction.",
    "ease_slew_cap_s_scroll":
        "The same glide speed limit as the line mode cap, but applied in scroll "
        "through belt layouts, where the whole belt rides the offset and a fast "
        "glide whooshes the text across the screen. Raise it to re-anchor the belt "
        "faster at the cost of visible lurching. Lower it for a smoother, slower "
        "slide. Typical 0.5 to 3.0, default 1.0. Read by the eased offset routine "
        "when a scroll layout is active.",
    "ease_snap_jump_s":
        "A correction larger than this many seconds is treated as a seek or a track "
        "change, so the display cuts straight to the new timing instead of gliding "
        "for a long time. Raise it and even big corrections glide, which looks "
        "smooth but drags. Lower it and more corrections cut instantly. Typical 5.0 "
        "to 20.0, default 12.0. Read by the eased offset routine and by the offset "
        "commit path.",
    "energy_apply_min":
        "The smallest change, in seconds, that the audio energy correlator is "
        "allowed to apply to the current offset on a studio track. Smaller results "
        "are treated as noise rather than a real measurement. Raise it to reject "
        "marginal corrections and avoid micro jitter. Lower it to let the "
        "correlator make finer adjustments. Typical 0.2 to 1.0, default 0.4. Read "
        "by the energy alignment routine.",
    "energy_lift_floor":
        "How far the best correlation peak must stand above the typical level "
        "before the app believes it, which is the confidence bar for energy based "
        "alignment. Raise it to accept only strong, obvious peaks, so false locks "
        "are rarer but a genuinely correct weak peak can be rejected. Lower it to "
        "accept weaker evidence. Typical 0.02 to 0.15, default 0.045. Read by the "
        "energy alignment routine and its confidence blend.",
    "energy_max_offset":
        "The largest offset, in seconds, that the energy correlator may propose on "
        "a studio track before the result is dismissed as implausible. Raise it to "
        "allow bigger corrections, which helps odd edits but admits more nonsense. "
        "Lower it to keep corrections conservative and reject wild results. Typical "
        "20 to 90, default 60.0. Read by the energy alignment routine and the "
        "screen reader sync range check.",
    "energy_max_offset_live":
        "The same plausibility cap on proposed offsets, but for live and concert "
        "arrangements, which legitimately need larger values because of crowd "
        "intros, stage talk, and tempo differences against a studio lyric file. "
        "Raise it for long concert videos with big lead ins. Lower it to reject "
        "implausible live jumps. Typical 60 to 200, default 120.0. Read by the "
        "energy alignment routine and the screen reader sync range check.",
    "energy_peak_margin":
        "How far ahead of the runner up the best correlation peak must score before "
        "the result is accepted. If a distant rival is within this margin, the "
        "reading is called ambiguous and rejected. Raise it to demand a clear "
        "winner, rejecting more results but avoiding chorus confusion. Lower it to "
        "accept closer calls. Typical 0.02 to 0.15, default 0.06. Read by the "
        "energy alignment ambiguity check.",
    "energy_shift_penalty":
        "A per second penalty applied to candidate offsets that move far from the "
        "current one, so the correlator prefers keeping existing sync unless the "
        "evidence is strong. Raise it to bias hard toward small adjustments and "
        "resist big jumps. Lower it, or set 0, to let distant candidates compete on "
        "raw score alone. Typical 0.0 to 0.05, default 0.012. Read by the energy "
        "alignment scoring step.",
    "fast_lock_max_s":
        "On a song already verified and title locked, a first reading this size or "
        "smaller in seconds is committed straight away rather than waiting for two "
        "point confirmation, because on a known correct song a modest offset is "
        "real drift. Raise it to lock bigger corrections quickly. Lower it so more "
        "corrections wait for confirmation. Typical 2.0 to 12.0, default 6.0. Read "
        "by the fast lock branch of the sync reader.",
    "fine_tune_enabled":
        "The master switch for the fine tune pass, a slow polishing loop that "
        "drives the remaining timing error down toward a fifth of a second once "
        "normal sync is already holding. Set 1 to enable the polish pass, or 0 to "
        "never run it and leave timing to the regular verification tier. Default 1. "
        "Read by the fine tune entry check, which returns immediately when this is "
        "0.",
    "fine_tune_enter_after_s":
        "How long, in wall clock seconds, sync must stay good before the fine tune "
        "polish pass engages. Raise it to require a longer settled run first, so "
        "fine tune starts rarely and later. Lower it to begin polishing sooner, "
        "which improves precision earlier but spends more CPU on listens. Typical "
        "10 to 60, default 20.0. Read by the fine tune entry check.",
    "fine_tune_exit_drift_s":
        "A measured timing error larger than this many seconds makes fine tune give "
        "up and hand the song back to the normal verification tier, which has the "
        "two point verifier for big moves. Keep it comfortably above the maximum "
        "pause cap so an error just under that cap is not bounced out. Raise it to "
        "keep polishing through bigger errors. Lower it to hand off sooner. Typical "
        "2.0 to 8.0, default 5.5. Read by the fine tune result handler.",
    "fine_tune_in_scroll":
        "Whether the fine tune polish pass is allowed to run in scroll through belt "
        "layouts. Set 1 to enable it there, or 0 to disable, which is the default "
        "because its sub second nudges only lurch a moving belt and each listen "
        "briefly stalls the rendering. Turn it on only if you want maximum "
        "precision in a scrolling layout and accept the cost. Default 0. Read by "
        "the fine tune entry check.",
    "fine_tune_inconclusive_exit":
        "How many consecutive unreadable listens fine tune tolerates before it "
        "exits and returns control to the normal verification tier. Raise it to "
        "keep trying through quiet or instrumental passages. Lower it to bail out "
        "quickly when the audio is not giving usable readings, saving CPU. Typical "
        "1 to 5, default 2. Read by the fine tune result handler when a listen "
        "returns nothing.",
    "fine_tune_listen_interval_s":
        "How many seconds pass between fine tune listens while the polish loop is "
        "running. Each listen briefly loads the CPU, so this sets both the "
        "correction rate and the cost. Raise it to poll less often and reduce load, "
        "which slows convergence. Lower it to converge faster at higher CPU cost. "
        "Typical 5.0 to 20.0, default 8.0. Read by the fine tune entry, listen "
        "tick, and result handler.",
    "fine_tune_max_move_ahead_s":
        "The largest catch up nudge fine tune may make when the lyrics are running "
        "behind the singing, applied by skipping forward. The cap is higher than "
        "the pause cap because a small forward skip is less noticeable than "
        "freezing the text. Raise it to catch up faster. Lower it for gentler "
        "nudges. Typical 0.5 to 4.0, default 2.0. Read by the fine tune result "
        "handler.",
    "fine_tune_max_pause_s":
        "The largest correction fine tune may make when the lyrics are running "
        "ahead of the singing, applied by briefly holding the lyric line still "
        "rather than jumping backwards. Raise it to fix bigger leads in one quiet "
        "hold. Lower it to keep holds short, handing larger errors back to the "
        "normal tier. Typical 1.0 to 6.0, default 5.0. Read by the fine tune result "
        "handler as the rewind magnitude cap.",
    "fine_tune_min_step_s":
        "The smallest adjustment fine tune will bother making. Errors below this "
        "many seconds are treated as already on target and left alone. Raise it to "
        "ignore more tiny errors, which keeps the display quieter. Lower it to act "
        "on finer errors, at the cost of more small movements the viewer may "
        "notice. Typical 0.1 to 0.5, default 0.2. Read by the fine tune result "
        "handler.",
    "fine_tune_target_s":
        "The timing error the fine tune pass aims for. Once the measured error is "
        "at or below this many seconds, the song is considered locked and no nudge "
        "is made. Raise it for a looser goal that finishes sooner and moves the "
        "lyrics less. Lower it to chase tighter precision with more frequent small "
        "adjustments. Typical 0.1 to 0.5, default 0.2. Read by the fine tune result "
        "handler.",
    "force_sync_agree_s":
        "How close a fresh Force Sync read must be to the offset currently being "
        "tried, in seconds, to count as still matching. Raise it and reads confirm "
        "more easily, so Force Sync settles faster but can accept a slightly wrong "
        "offset. Lower it to demand tighter agreement, which is more accurate but "
        "may never confirm on noisy audio. Typical 0.5 to 2.0, default 1.0. Read by "
        "the Force Sync confirm routine.",
    "force_sync_listen_s":
        "How many seconds of audio each Force Sync read captures for transcription. "
        "A longer capture gives the matcher more words to work with, so ranked "
        "candidates are better, but each attempt takes longer and uses more CPU. "
        "Raise it when Force Sync keeps failing to find a match. Lower it for "
        "quicker attempts. Typical 5.0 to 15.0, default 8.0. Read by the Force Sync "
        "read scheduler.",
    "force_sync_span_s":
        "The confirming reads must be spread across at least this many seconds of "
        "playback before Force Sync locks, so it cannot lock inside a single pass "
        "of a repeating chorus. Raise it to force confirmations further apart, "
        "which is safer and slower. Lower it to lock sooner with more chorus risk. "
        "Typical 8.0 to 30.0, default 16.0. Read by the Force Sync engage and "
        "confirm routines.",
    "force_sync_streak":
        "During a manual Force Sync, how many confirming reads in a row a candidate "
        "offset must survive before it is locked in. Raise it to demand stronger "
        "proof, which makes a wrong lock very unlikely but takes longer to settle. "
        "Lower it to lock faster with more risk of settling on a repeated chorus. "
        "Typical 2 to 5, default 3. Read by the Force Sync engage and confirm "
        "routines.",
    "force_sync_top_n":
        "How many candidate offsets each Force Sync read ranks and keeps, tried "
        "best first and falling through to the next when one fails to hold. Raise "
        "it to give Force Sync more fallbacks on an awkward track, at the cost of a "
        "longer search. Lower it to try only the strongest guesses and give up "
        "sooner. Typical 3 to 10, default 6. Read by the Force Sync read scheduler.",
    "gaming_sync_hard_drift_s":
        "While gaming tight sync is on, a measured timing error larger than this "
        "many seconds forces an immediate listen and realign, bypassing the normal "
        "cadence. Raise it to tolerate more drift before the emergency correction "
        "fires, saving CPU. Lower it to react sooner, at the cost of more "
        "recognition work during play. Typical 0.8 to 3.0, default 1.5. Read by the "
        "gaming hard drift watchdog.",
    "gaming_sync_hard_min_gap_s":
        "The minimum seconds between two hard drift forced realigns, which stops a "
        "genuine slip from triggering a storm of transcription work while a game is "
        "running. Raise it to rate limit harder and protect game performance. Lower "
        "it to allow emergency corrections closer together. Typical 2.0 to 15.0, "
        "default 3.0. Read by the gaming hard drift watchdog before forcing an "
        "align.",
    "gaming_sync_tight":
        "Whether the sync engine tightens up while the Gaming preset is active. "
        "Gaming shows a single line, where any misalignment is obvious, so tight "
        "mode refuses to relax the resync cadence, halves the drift trigger, and "
        "enables the hard drift watchdog. Set 1 to enable this behaviour, or 0 to "
        "keep the normal cadence while gaming. Default 1. Read by the resync "
        "cadence controller, the drift accumulator, and the hard drift watchdog.",
    "gen_model_dl_timeout_s":
        "The longer wait, in seconds, allowed when the speech model still has to be "
        "downloaded, which normally happens only on the first run. Raise it for a "
        "slow connection so a large download is not cut short. Lower it to give up "
        "faster on a stalled download. Typical 120 to 900, default 240.0. Read by "
        "the generation stall watchdog in main.py, in place of gen_stall_timeout_s.",
    "gen_stall_timeout_s":
        "How long the app waits, in seconds, on a lyric generation that has "
        "produced nothing at all before abandoning it and retrying the normal "
        "online fetch. Raise it to give a slow machine more time to produce a first "
        "result. Lower it to give up sooner rather than sitting on a Generating "
        "message. Typical 30 to 180, default 75.0. Read by the generation stall "
        "watchdog in main.py.",
    "gpu_renderer_on":
        "Chooses which drawing engine paints the lyrics. Set 1 to enable the "
        "separate graphics accelerated overlay window, which draws on the graphics "
        "card at high frame rates with per pixel transparency and click through. "
        "Set 0 to disable and use the built in window instead. Default 0, and it "
        "can be flipped while running. Read by the renderer startup and switch code "
        "in main.py.",
    "gpu_solo_override":
        "Whether transcription may use the graphics card on a machine that only has "
        "one. Set 1 to enable, using that single card for speed and accepting that "
        "heavy work shares it with games and browsers. Set 0 to disable, the safe "
        "default, keeping this work on the processor. Machines with several cards "
        "are unaffected. Default 0. Read by the GPU selection policy in "
        "gpu_setup.py and align.py.",
    "identify_respects_jank_backoff":
        "Whether song identification pauses itself after the display has stuttered. "
        "Set 1 so a listen is skipped while the smoothness backoff is active, "
        "protecting frame rate at the cost of slower identification. Set 0 to "
        "listen regardless of recent stutter. A still-unidentified song caps the "
        "backoff short either way. Default 1. Read by the identify scheduler.",
    "keep_last_line_gap_s":
        "In the CPU renderer, the previous lyric line stays on screen through a gap "
        "between lines shorter than this many seconds, which stops the text "
        "flickering off and back on. Raise it to hold lines through longer gaps, at "
        "the risk of showing a finished line into a real pause. Lower it to clear "
        "the screen sooner. Typical 0.2 to 2.0, default 0.6. Read by the active "
        "line index change handler.",
    "live_energy_apply_min":
        "The smallest timing change, in seconds, that the loudness-matching sync is "
        "allowed to actually apply during a live arrangement or concert. Raise it "
        "to ignore small drift so the lyrics do not twitch, at the cost of leaving "
        "them slightly off. Lower it to correct finer drift, keeping the lyrics "
        "tighter but nudging them more often. Typical 0.05 to 0.6, default 0.15. "
        "Read by the automatic energy alignment pass in main.py.",
    "live_energy_lift_floor":
        "How far the best loudness-match must stand above the average match before "
        "a live or concert timing offset is believed. Raise it so only sharp, "
        "obvious peaks are trusted, which avoids false locks but can mean noisy "
        "crowd audio never locks at all. Lower it to accept weak peaks, so a re- "
        "sung song over crowd noise locks more often but with more wrong locks. "
        "Typical 0.01 to 0.1, default 0.025. Also anchors the confidence blend when "
        "the result applies.",
    "live_energy_peak_margin":
        "How far ahead of the next best distant match the winning loudness-match "
        "must be, for live and concert audio, before the result is trusted. Raise "
        "it to reject more results as ambiguous, which guards against locking onto "
        "the wrong repeat of a chorus. Lower it to accept closer calls, so more "
        "corrections land but chorus mix-ups become likelier. Typical 0.01 to 0.15, "
        "default 0.035. Read by the automatic energy alignment pass in main.py.",
    "live_max_jump_s":
        "Biggest sudden timing jump, in seconds, that a live song is allowed to "
        "make away from an offset that had already settled. Raise it to let "
        "genuinely large re-syncs through, at the risk of the app locking onto a "
        "repeated chorus. Lower it to keep a settled offset stubbornly still, at "
        "the risk of refusing a correction the song really needed. Typical 20 to "
        "120, default 45. Read by the live sync-follow branch of the audio identify "
        "path in main.py.",
    "live_resync_fast_gap_s":
        "The pause after each resync listen while the app is in its most aggressive "
        "mode, used when a song has just started or a reading just failed. Raise it "
        "to check less often and use less processor time. Lower it to check more "
        "often so a drifting concert is hammered back into place sooner, at a "
        "higher processing cost. Typical 0.5 to 4, default 1.0. Read by the live "
        "resync loop and its cadence roller in main.py.",
    "live_resync_listen_s":
        "How many seconds of audio are recorded for each by-ear resync check during "
        "a live set or concert. Raise it to capture more sung words per check, "
        "which makes each match more reliable but costs more processing time and "
        "stretches the cycle. Lower it for quicker, cheaper cycles that miss more "
        "often. Typical 2 to 10, default 4. Read by the live resync loop and by the "
        "gaming hard-drift force-align in main.py.",
    "live_resync_mid_gap_s":
        "The pause after each resync listen once the app has had a run of "
        "successful readings and has relaxed one step. Raise it to back off harder "
        "once the song is behaving, saving processing time but noticing new drift "
        "later. Lower it to keep checking briskly even when the song looks locked. "
        "Typical 3 to 12, default 6. Read by the live resync cadence roller, which "
        "picks this tier after the relax count is met.",
    "live_resync_relax_n":
        "How many good readings in a row are needed before the resync rhythm "
        "relaxes one step, from fast to medium and then medium to slow. Any failed "
        "reading resets the count and snaps back to fast. Raise it to stay in the "
        "aggressive rhythm much longer before easing off. Lower it to ease off "
        "after only a reading or two. Typical 1 to 8, default 3. Read by the live "
        "resync cadence roller in main.py.",
    "live_resync_s":
        "A leftover from the older fixed-cadence live resync loop. It is still "
        "registered so it can be set, but no code reads it any more, so raising it "
        "does nothing and lowering it does nothing. The live and concert resync "
        "rhythm is now set by the listen length plus the fast, mid and slow gap "
        "knobs below. Default 6.0. Declared in the tune table in main.py near line "
        "3072, with no reader anywhere in the app.",
    "live_resync_slow_gap_s":
        "The pause after each resync listen once the song is considered fully "
        "locked in, after twice the relax count of good readings. Raise it to check "
        "very rarely while things hold, which is cheapest but slowest to spot fresh "
        "drift. Lower it to keep a safety margin of regular checks even on a well "
        "behaved song. Typical 8 to 30, default 14. Read by the live resync cadence "
        "roller in main.py.",
    "live_single_shot_max_s":
        "For a live arrangement, the largest timing correction in seconds that is "
        "applied straight away from one reading, with no confirming second reading. "
        "Raise it and more corrections land immediately, so the song locks faster "
        "but a bad reading can move it wrongly. Lower it and more corrections must "
        "wait to be paired up, which is safer but can mean the song never locks at "
        "all. Typical 0 to 3, default 1.2. Read by the sync tier drift handler.",
    "live_song_max_s":
        "In a live set or concert, the media player reports the whole video length "
        "rather than the current song. A length above this many seconds is treated "
        "as a container length and dropped before lyrics are fetched. Raise it and "
        "long durations reach the providers, which then reject correct short-song "
        "lyrics. Lower it and more are dropped so the fetch matches on title and "
        "artist only. Typical 300 to 1800, default 900. Read by the fetch starter.",
    "live_sync_match_min":
        "How closely the words the app heard must match the loaded lyric text, from "
        "0 to 1, before a large timing shift is trusted on a live or cover version. "
        "Raise it towards the studio bar and only near exact matches move the "
        "timing, which is safer but can leave a re-sung song out of time all the "
        "way through. Lower it to accept the looser match a live performance gives. "
        "Typical 0.4 to 0.8, default 0.62. Read by the sync match floor helper.",
    "live_tpvr_gap_s":
        "For a live arrangement of a single song, how many seconds to wait between "
        "the first timing reading and the confirming second reading. Raise it so "
        "the two readings land further apart in the song, which stops a repeated "
        "chorus being read twice at the same wrong place, but locking takes longer. "
        "Lower it to lock faster and accept more chorus confusion. Typical 1 to 6, "
        "default 2.5. Read by the two-point verification stage of the sync tier.",
    "lyrics_blacklist_max":
        "How many rejected lyric bodies are remembered for the current song, so a "
        "re-fetch cannot hand back the same wrong words. Raise it to rule out more "
        "bad candidates during a stubborn song. Lower it to bound memory and avoid "
        "starving every provider during a storm of corrections. Cleared on every "
        "track change. Typical 4 to 20, default 8. Read by the blacklist helper.",
    "measure_text_cache_size":
        "How many measured character widths are remembered so text does not have to "
        "be measured over and over while drawing. Raise it if a song uses many "
        "characters or font sizes and the cache keeps missing. Lower it to use less "
        "memory, accepting more repeated measuring work. Typical 1024 to 16384, "
        "default 4096. Read once at startup by the shared measurement cache in "
        "main.py, so a change needs a restart.",
    "mv_intro_timeout":
        "A backstop, in seconds, after which the waiting for vocals intro card "
        "releases even if no vocal has been detected. The normal release comes from "
        "the vocal detector, so this only matters on an unusual intro. Raise it for "
        "videos with very long openings, at the risk of holding the card over real "
        "singing. Lower it to release sooner. Typical 10 to 80, default 20.0. Read "
        "by the intro hold logic in the main tick.",
    "notable_events_enabled":
        "Whether a running plain language account of what the app is doing is "
        "recorded for the console log panel. Set 1 to enable, the default, which "
        "makes it far easier to see why lyrics changed or moved. Set 0 to disable "
        "and record nothing, saving a very small amount of work. Default 1. Read by "
        "the event recorder in main.py before storing each entry.",
    "notable_events_size":
        "How many recent narrative events are kept before the oldest ones are "
        "discarded. Raise it to keep more history visible in the console panel when "
        "investigating something that happened a while ago. Lower it to use less "
        "memory and keep the panel short. Typical 30 to 500, default 120. Read by "
        "the event recorder in main.py each time it stores an entry.",
    "notable_sync_ignored_streak":
        "How many timing corrections in a row must be discarded as too small before "
        "the log says so. Raise it to stay quiet through longer runs of rejected "
        "corrections. Lower it to be warned sooner that the lyrics look off and the "
        "engine keeps failing to fix them. Typical 2 to 20, default 5, and values "
        "below 1 are clamped up. Read by the sync correction path in main.py.",
    "notable_sync_min_s":
        "The smallest timing correction, in seconds, that is worth mentioning in "
        "the narrative log. Raise it to hide small adjustments and keep the console "
        "quiet. Lower it, or leave it at 0.0 which is the default, so every "
        "correction that actually moved the lyrics on screen gets reported. Typical "
        "0.0 to 1.0. Read by the sync narration helper in main.py.",
    "ocr_setlist_gate":
        "Set 1 to enable, 0 to disable. When a concert's song list is known, 1 "
        "makes the on-screen banner reader match only against those songs. The "
        "capture covers the whole browser window, so with 0 leftover search box "
        "text or an advert can score against your library and load as if it were a "
        "song. Turn it off only to restore the older, unrestricted matching. "
        "Default 1. Read by the concert banner reading pass in main.py.",
    "ocr_sync_in_live":
        "Set 1 to enable, 0 to disable. With 1 the screen reader is allowed to "
        "correct lyric timing during a concert or live set, which matters because "
        "burned-in on-screen lyrics carry the real timing of that performance while "
        "the studio lyric file does not. With 0 it is blocked in live mode and only "
        "ordinary songs get screen-assisted timing. Default 1. Read by the OCR "
        "assisted sync routine in main.py.",
    "ocr_sync_min":
        "How closely a line read off the screen must match a line in the loaded "
        "lyrics, from 0 to 1, before it is used to set the timing on an ordinary "
        "studio song. Raise it so only strong matches move the timing, which is "
        "safer but means fewer corrections land. Lower it to accept weaker matches, "
        "so timing is corrected more often but a wrong line can drag the song to "
        "the wrong place. Typical 0.4 to 0.9, default 0.66.",
    "ocr_sync_min_live":
        "The same on-screen to lyric-file match bar, but for live and concert "
        "performances, where ad-libs and re-sung phrasing make the on-screen text "
        "differ from the studio lyric file. Raise it towards the studio bar and few "
        "live corrections will pass. Lower it to accept the looser match live text "
        "gives, which corrects more often but risks matching the wrong line. "
        "Typical 0.4 to 0.85, default 0.58. Read by the OCR assisted sync routine.",
    "ocr_sync_single_shot_max":
        "The largest timing correction, in seconds, the screen reader may apply "
        "from a single reading. Anything bigger must be backed up by a second "
        "reading of a different lyric line that agrees. Raise it so large "
        "corrections land quickly, at the risk of one bad reading throwing the song "
        "far into the wrong part of itself. Lower it to demand corroboration for "
        "more corrections. Typical 1 to 10, default 4. Read by the OCR assisted "
        "sync routine.",
    "ocr_when_gaming":
        "Set 1 to enable, 0 to disable. With 1 the app may keep reading text off "
        "your screen, which uses the graphics card, even while a game has that card "
        "busy. With 0 it stands down so it cannot cause stutter in the game. "
        "Machines with two or more graphics cards always allow it regardless, since "
        "the spare card has headroom. Default 0. Read by the OCR safety check that "
        "guards every screen reading path in main.py.",
    "offset_defer_cap_s":
        "How long a queued timing correction may wait for the current lyric line to "
        "end before it is committed anyway. It bounds how long the display stays "
        "knowingly wrong while being polite about line boundaries. Raise it to wait "
        "longer for a clean boundary. Lower it to apply corrections sooner even mid "
        "line. Typical 1.0 to 8.0, default 3.0. Read by the pending offset commit "
        "step in the main tick.",
    "onset_max_intro_s":
        "The longest instrumental intro, in seconds, that the app will believe when "
        "it detects the first vocal of a music video. A vocal onset later than "
        "this, past half the video, or past the end of the lyrics, is treated as a "
        "mid song false trigger and ignored. Raise it for videos with very long "
        "openings. Lower it to reject suspicious onsets sooner. Typical 30 to 150, "
        "default 90.0. Read by the vocal onset handler.",
    "overlay_heartbeat_stale_s":
        "How many seconds without a sign of life from the separate graphics overlay "
        "window before it is treated as dead and the built in display takes over. "
        "Raise it to tolerate longer freezes before switching back. Lower it to "
        "recover faster, with more risk of switching during a brief hiccup. Typical "
        "2 to 20, default 6.0. Read by the overlay watchdog in main.py.",
    "perf_record":
        "Turns on per frame performance logging to a perf.log file for diagnosing "
        "stutter. Set 1 to enable, which appends a timing row every frame with near "
        "zero measurable slowdown. Set 0 to disable, the normal state for everyday "
        "use, writing nothing at all. Default 0. Read by the render tick perf "
        "recorder in main.py before every log write.",
    "perf_record_branches":
        "Which sections of the frame get individually timed in the performance log, "
        "given as names separated by vertical bars. Adding branches gives finer "
        "detail about where a slow frame went. Removing them trims the tiny "
        "measuring overhead once a section has been cleared of blame. Default "
        "covers render, kara and itemconfig; unknown names are ignored. Read by the "
        "per branch timer in main.py.",
    "perf_record_cap_mb":
        "Size limit in megabytes for the performance log before it is rotated and "
        "started fresh. Raise it to keep a longer history when hunting a rare "
        "stutter that takes a while to reproduce. Lower it to protect disk space, "
        "at the cost of losing older frames sooner. Typical 5 to 200, default 20.0. "
        "Read by the perf recorder in main.py each time it checks the log size.",
    "perf_record_path":
        "Where the performance log is written. Give an explicit file path as text "
        "to send the log somewhere specific, such as a fast scratch disk. Leave it "
        "empty, the default, and the app writes perf.log into its own data folder "
        "whenever perf_record is 1. There is no numeric range; it is a path string, "
        "default empty. Read by the perf recorder in main.py when it opens the log.",
    "perf_record_raw_frame_ms":
        "Adds a raw, unsmoothed frame time column to the performance log. Set 1 to "
        "enable, so a single long stall stands out clearly instead of being blurred "
        "by the running average. Set 0 to disable and keep the older log layout "
        "with the averaged figure only. Default 1. Read by the perf recorder in "
        "main.py when it formats each row.",
    "pinned_auto_migrate_same_app":
        "What happens when a pinned audio source vanishes and exactly one other "
        "session belongs to the same application. Set 1 to enable, moving the pin "
        "to that session so lyrics keep following, for example when a video site "
        "autoplays the next track. Set 0 to disable, holding the pin until it "
        "expires. Default 1. Read by the pinned session logic in main.py.",
    "pinned_grace_s":
        "How long a pinned audio source is kept, in seconds, after it disappears, "
        "before the app reverts to choosing a source automatically. Raise it to "
        "survive longer interruptions such as a page reload or a gap between "
        "videos. Lower it to return to automatic selection sooner. Typical 5 to "
        "120, default 30.0. Read by the pinned session logic in main.py.",
    "pinned_menu_refresh_s":
        "Intended as the minimum wait, in seconds, between rebuilds of the tray "
        "Source submenu when the set of visible audio sessions changes, so a "
        "churning list cannot rebuild the menu constantly. Raising it would rebuild "
        "less often, lowering it more often. Typical 0.5 to 10, default 2.0. Note "
        "that no code reading this key was found; only its registration in the tune "
        "table in main.py.",
    "pos_stale_thresh_s":
        "How long the reported playback position may sit unchanged, in seconds, "
        "while the player still says it is playing, before the app assumes the "
        "report is stuck and estimates position from the clock instead. Raise it to "
        "trust a sluggish player longer. Lower it so lyrics stop looking frozen "
        "sooner, with more risk of estimating needlessly. Typical 0.5 to 5, default "
        "1.5. Read by the playback tick in main.py.",
    "prefer_audible_session":
        "How ties are broken when several media sessions all claim to be playing, "
        "such as two browser tabs. Set 1 to enable, the default, preferring "
        "whichever program is actually making sound according to the system audio "
        "meters. Set 0 to disable and use the older behaviour of sticking with the "
        "previous choice. Default 1. Read by the audio session selection path in "
        "main.py.",
    "prefer_audible_threshold":
        "The loudness level below which a program counts as silent when breaking "
        "ties between playing sessions, on a scale where 1.0 is maximum. Raise it "
        "so only clearly audible sources qualify, which can wrongly skip very quiet "
        "passages. Lower it to count faint sound as playing, risking picking a near "
        "silent tab. Typical 0.001 to 0.05, default 0.005, about 46 decibels below "
        "full scale. Read by the audio session selection path in main.py.",
    "recal_critical_backoff_s":
        "The longer pause, in seconds, applied to automatic sound checks after a "
        "severe stutter rather than a mild one. Raise it to back off harder and "
        "keep playback calm for longer. Lower it so the app returns to checking "
        "sync more quickly after a big hitch. Typical 15 to 180, default 60.0. Read "
        "by the frame timing watchdog in main.py.",
    "recal_jank_backoff_s":
        "How many seconds automatic sound checks are paused after an ordinary "
        "stutter, so the app stops adding load while things are rough. Raise it for "
        "a longer quiet period and smoother playback, at the cost of slower drift "
        "correction. Lower it to resume checking sooner. Typical 5 to 60, default "
        "20.0. Read by the frame timing watchdog in main.py.",
    "recognize_in_process_fallback":
        "What happens when the separate song-recognition helper process cannot "
        "start or crashes. Set 1 to fall back to running the recognizer inside the "
        "main app, which keeps identification working but can stutter playback and "
        "the lyric animation. Set 0 to abandon that listen instead and protect "
        "smoothness. Default 0. Read by the identify worker thread.",
    "reset_offset_max":
        "The ambiguity reset above is only allowed while the current offset is "
        "smaller than this many seconds, so a large hard won correction is never "
        "thrown away by scattered reads. Raise it and bigger offsets also become "
        "resettable, which recovers from bad corrections but can undo good ones. "
        "Lower it to protect large offsets. Typical 2.0 to 10.0, default 5.0. Read "
        "by the studio branch of the Shazam sync reader.",
    "scroll_fill_interval":
        "The minimum wait in seconds between highlight repaints of the same "
        "scrolling line. Raise it to repaint each line less often, saving work but "
        "making the colour sweep look stepped. Lower it, or leave it at 0.0 which "
        "is the default, so the sweep can update every frame and glides. Typical "
        "0.0 to 0.05. Read only by the scroll mode render path in main.py.",
    "scroll_fill_skip":
        "How often the heavier drawing pass runs while lyrics scroll, expressed as "
        "every Nth frame. Raise it to skip more frames, cutting drawing work "
        "sharply if the belt struggles, though highlights then update less often. "
        "Lower it toward 1 so the pass runs every frame. Typical 1 to 3, default "
        "1.0. Read only by the scroll mode render path in main.py.",
    "scroll_heavy_budget_ms":
        "A time budget in milliseconds for how much lyric drawing work may happen "
        "in one frame while lyrics scroll across the screen. Raise it for quicker "
        "filling and fewer gaps on a fast machine. Lower it if the scroll belt "
        "stutters, since drawing steals time from motion. Typical 6 to 25, default "
        "14.0, and 0 removes the cap. Read only by the scroll mode render path in "
        "main.py.",
    "scroll_max_lanes":
        "How many scrolling lyric lines may be stacked on screen at once, subject "
        "to what actually fits. Raise it to show more upcoming words together, "
        "which costs noticeably more drawing work since bitmap area is the dominant "
        "scroll cost. Lower it for a lighter, cleaner display that is easier on a "
        "slow machine. Typical 1 to 4, default 3. Read by the scroll layout code in "
        "main.py.",
    "scroll_motion_back_cap":
        "A multiplier controlling how fast the scrolling belt may travel backwards "
        "during a downward sync correction. Raise it above 0 to allow a bounded "
        "reverse slide, which reaches the right place sooner but looks like the "
        "lyrics going back and forth. Keep it at 0.0, the default, so the belt "
        "simply pauses instead of reversing. Typical 0.0 to 1.0. Read by the scroll "
        "belt motion clock in main.py.",
    "scroll_motion_back_snap_s":
        "How large a backward jump, in seconds, forces the belt to cut cleanly "
        "instead of freezing in place. Because the belt refuses to run in reverse "
        "by default, this also bounds how long it can sit still, roughly this many "
        "seconds. Raise it to allow longer pauses before a cut. Lower it to cut "
        "sooner and keep lyrics moving. Typical 1 to 5, default 2.0. Read by the "
        "scroll belt motion clock in main.py.",
    "scroll_motion_catchup":
        "Extra speed the scrolling belt may use to catch up after a stall or a sync "
        "nudge, measured as additional seconds of travel per real second. Raise it "
        "to close a gap faster, at the risk of a visible rush. Lower it so recovery "
        "is gentle and the belt takes longer to get back on time. Typical 1 to 8, "
        "default 4.0. Read by the scroll belt motion clock in main.py.",
    "scroll_motion_pull_per_sec":
        "How stiffly the scrolling belt glides forward toward its target position "
        "inside the separate overlay window, where higher means a firmer pull. "
        "Raise it to catch up more quickly with a tighter feel. Lower it for a "
        "looser, floatier glide that takes longer to settle. Typical 2 to 12, "
        "default 6.0. Read by the overlay child process belt clock; main.py only "
        "forwards the value in the state it sends.",
    "scroll_motion_seek_snap_s":
        "How big a jump in seconds counts as a real seek or track change rather "
        "than a small correction. Above this the belt cuts straight to the new "
        "place instead of gliding. Raise it to glide through larger corrections, "
        "which can look like a long slide. Lower it so the belt cuts more often and "
        "arrives sooner. Typical 2 to 10, default 4.0. Read by the scroll belt "
        "motion clock in main.py.",
    "scroll_repaint_budget":
        "How many karaoke highlight repaints may happen in one frame while lyrics "
        "scroll. Raise it so more visible lines update their colour fill together, "
        "which looks crisper on a capable machine. Lower it if scrolling stutters, "
        "because each repaint costs frame time. Typical 2 to 12, default 4. Read "
        "only by the scroll mode render path in main.py, never by line mode.",
    "scroll_spawn_budget":
        "How many brand new lyric blocks may be drawn from scratch in one frame "
        "while lyrics scroll. Raise it to build upcoming lines further ahead, which "
        "helps when many lines arrive at once. Lower it because building a block "
        "allocates memory and can spike frame time, the usual cause of a visible "
        "lurch. Typical 1 to 3, default 1. Read only by the scroll mode render path "
        "in main.py.",
    "scroll_spawn_margin":
        "How far off the edge of the screen, in pixels, a lyric block is prepared "
        "before it scrolls into view. Raise it to build lines further ahead so "
        "nothing visibly pops in, at the cost of preparing more than may be needed. "
        "Lower it to do less speculative work, with more risk of a line appearing "
        "abruptly. Typical 400 to 2000, default 1100. Read by the scroll mode "
        "render path in main.py.",
    "scroll_v_stagger":
        "For vertical scrolling, how far apart sideways the stacked columns of "
        "lines sit, in pixels at normal scale, so lines cascade diagonally instead "
        "of stacking in one column. Raise it to fan the lines wider across the "
        "screen. Lower it to bring them closer to a single column. Typical 0 to "
        "500, default 250, and it scales with font size. Read by the scroll layout "
        "code in main.py.",
    "setlist_gen_deadline_s":
        "How long a concert chapter is given to find real lyrics before the app "
        "gives up and generates them by ear under that chapter's title. Raise it to "
        "allow more time for a slow provider fetch to land, leaving the screen bare "
        "for longer. Lower it so generated lyrics appear sooner, at the risk of "
        "pre-empting a fetch that was about to succeed. Typical 20 to 120, default "
        "45. Read by the concert setlist tick, which arms the generation backstop.",
    "shazam_lock_grace":
        "After a confirmed audio fingerprint lock, automatic realign passes are "
        "skipped for this many seconds, because a fresh authoritative lock is "
        "better evidence than another guess. Raise it to trust a lock longer and "
        "cut redundant listening. Lower it to allow re-checking sooner after a "
        "lock. Typical 15 to 60, default 30.0. Read by align by listening, which "
        "returns early inside the grace period.",
    "single_shot_max_s":
        "On a studio track, a timing correction this size or smaller is applied "
        "from a single listen instead of waiting for a second agreeing one. Small "
        "corrections are low risk, while a wrong chorus match shows up as a large "
        "one. Raise it to commit bigger jumps immediately, which is faster but "
        "riskier. Lower it so more corrections need pairing. Typical 1.0 to 3.0, "
        "default 2.0. Read by the sync tier listen path.",
    "smooth_fill":
        "Decides what drives the karaoke colour sweep across each line. Set 1 to "
        "enable, running the sweep from its own steady clock so a sync correction "
        "can never jerk it in the middle of a line. Set 0 to disable and tie the "
        "sweep directly to the sync position, the older behaviour. Default 1. Read "
        "by the fill fraction code in main.py and forwarded to the GPU overlay.",
    "smooth_fill_snap":
        "How far the smooth colour sweep may drift from the true position, as a "
        "fraction of a line, before it jumps rather than glides. Raise it to "
        "tolerate bigger differences and glide through more corrections. Lower it "
        "so the sweep snaps to the right place sooner, which is more accurate but "
        "more visible. Typical 0.1 to 0.6, default 0.34. Read by the fill fraction "
        "code in main.py.",
    "smoothness_bad_frame_ms":
        "How long a single frame must take, in milliseconds, before the app counts "
        "it as a stutter. Raise it to be more forgiving, so only really bad hitches "
        "are flagged and background song checks are paused less often. Lower it to "
        "catch milder roughness sooner. Typical 40 to 150, default 70.0. Read by "
        "the frame timing watchdog and the diagnostics summary in main.py.",
    "smoothness_critical_frame_ms":
        "The frame time in milliseconds that counts as a severe hitch rather than "
        "an ordinary one, which triggers the longer recovery pause. Raise it so "
        "fewer stalls are treated as severe and song checks resume sooner. Lower it "
        "to react harder to moderate stalls. Typical 100 to 400, default 180.0. "
        "Read by the frame timing watchdog and diagnostics in main.py.",
    "smoothness_kill_identify":
        "Whether a severe stutter is allowed to cancel the song identification "
        "running at that moment. Set 1 to enable, so listening is abandoned and "
        "playback smooths out again. Set 0 to disable, letting identification "
        "always run to completion even if the picture keeps hitching. Default 1. "
        "Read by the frame timing watchdog in main.py.",
    "smoothness_kill_identify_ms":
        "The frame time in milliseconds that is bad enough to abort a song "
        "identification already in progress, so playback stops glitching. Raise it "
        "to let identification finish through heavier stalls. Lower it to protect "
        "smoothness more aggressively, at the cost of more abandoned song checks. "
        "Typical 150 to 500, default 220.0. Read by the frame timing watchdog in "
        "main.py.",
    "smoothness_recent_window_s":
        "How many seconds of recent frame history the smoothness report looks at "
        "when judging whether things are rough right now. Raise it for a steadier "
        "reading that ignores one off blips. Lower it to make the report react "
        "quickly to what just happened. Typical 3 to 30, default 8.0, and values "
        "below 1 are clamped up. Read by the diagnostics smoothness summary in "
        "main.py.",
    "smtc_paused_min_s":
        "How long the media player must stay continuously not-playing before the "
        "heard-song takeover is allowed to fire. Raise it to demand a longer, more "
        "certain pause so a brief buffering blip cannot hand control to the "
        "listener. Lower it to react sooner when a paused tab is clearly not the "
        "audio in the room. Typical 4 to 20 seconds, default 8.0. Read by the "
        "source-priority resolver.",
    "smtc_paused_shazam_takeover":
        "Master switch for letting what the app HEARS beat a paused media session. "
        "When Windows reports the player is paused or stopped but the listener "
        "keeps hearing a different song, the paused tab cannot be what is audible "
        "in the room. Set 1 to enable the takeover, 0 to disable it so paused- "
        "player lyrics stay loaded and only decide-by-ear can override. Default 1. "
        "Read by the source-priority resolver.",
    "smtc_takeover_debounce_s":
        "Minimum seconds between two consecutive paused-player takeovers, so a "
        "fresh swap gets time to settle before another can fire. Raise it to stop "
        "the app ping-ponging between songs on noisy listening results. Lower it to "
        "allow quicker successive corrections. A real unpause bypasses this "
        "entirely. Typical 10 to 60 seconds, default 20.0. Read by the source- "
        "priority resolver.",
    "spread_reset":
        "How far apart, in seconds, recent listen results must scatter before the "
        "app decides the song is ambiguous, usually a track with repeated choruses, "
        "and resets the offset instead of chasing contradictory readings. Raise it "
        "to tolerate wilder scatter before giving up and resetting. Lower it to "
        "bail out to a clean zero offset sooner. Typical 10 to 40, default 20.0. "
        "Read by the studio branch of the Shazam sync reader.",
    "subs_ocr_band":
        "Names the slice of the window the subtitle reader crops before looking for "
        "text. The anime_sub preset targets the lower middle of a browser window, "
        "where the video sits above a site's download panel and comments. The lyric "
        "preset is the older, higher band used for concert banners. This is a name, "
        "not a number, so there is nothing to raise or lower; you pick one of the "
        "presets. Default anime_sub. Read by the subtitle OCR poll loop and its "
        "starter.",
    "subs_ocr_enable":
        "Master switch for reading burned-in anime subtitles off the screen. Set 1 "
        "to enable, 0 to disable. With 1, when Subtitles mode is on, the video is "
        "on a known anime streaming site and no downloadable caption track exists, "
        "the app polls the screen and shows the lines it reads. With 0 it skips "
        "that entirely and falls through to generating a transcript by ear. Default "
        "1. Read by the subtitles no-captions branch in main.py.",
    "subs_ocr_hard_cap_s":
        "How long the anime subtitle reader keeps polling before it stops by "
        "itself, in seconds, with 0 meaning no limit at all. Raise it above zero to "
        "make the reader shut down after that long, saving power on a machine left "
        "running. Lower it back to 0 so the reader stays up for a whole episode, "
        "which commonly runs past twenty minutes. Typical 0, or 300 to 3600 if you "
        "want a cap. Default 0. Read by the subtitle OCR poll loop.",
    "subs_ocr_interval_s":
        "How many seconds pass between screen reads while the anime subtitle reader "
        "is running. Raise it to read less often, which is lighter on the processor "
        "and graphics card but lets short lines slip past unseen. Lower it to catch "
        "quick lines and fast dialogue, at a noticeably higher load. Values below "
        "0.15 are clamped up. Typical 0.2 to 1.5, default 0.40. Read by the "
        "subtitle OCR poll loop in main.py.",
    "subs_ocr_log_every":
        "How often, counted in screen reads, the subtitle reader records a sample "
        "of the raw text it is seeing into the log, up until the first line is "
        "accepted. Raise it for a quieter log, which makes a badly aimed reading "
        "region harder to spot. Lower it for frequent samples that show exactly "
        "what the reader sees, at the cost of a noisy log. Typical 1 to 200, "
        "default 20. Read by the subtitle OCR poll loop in main.py.",
    "subs_ocr_stable_polls":
        "How many screen reads in a row must produce the same subtitle text before "
        "that line is shown on the overlay. Raise it to demand repeats, which "
        "filters out garbled or half-drawn reads but delays every line by that many "
        "polls. Lower it, and 1 is the minimum, to show a line the moment it is "
        "first read. Typical 1 to 4, default 1. Read when the subtitle harvester is "
        "created in main.py and enforced inside the OCR harvester.",
    "sung_prewarm":
        "Whether the highlighted version of an upcoming line is prepared in advance "
        "using spare time in earlier frames. Set 1 to enable, so the first "
        "highlighted frame does not have to draw all the glyphs at once and the "
        "sweep starts smoothly. Set 0 to disable and build it lazily at first use. "
        "Default 1. Read by the scroll mode render path in main.py.",
    "sung_prewarm_lead_s":
        "How many seconds before a line starts that its highlighted version is "
        "prepared in advance. Raise it to warm up earlier, giving more slack on a "
        "slow machine at the cost of holding prepared images longer. Lower it to do "
        "the work closer to when it is needed. Typical 0.5 to 5, default 2.0. Read "
        "by the scroll mode render path in main.py when sung_prewarm is 1.",
    "swap_defer_enabled":
        "Whether replacing the lyric body waits for a natural gap. Set 1 to keep "
        "the current words on screen while the new ones are fetched in the "
        "background, then swap at the end of a line or during an instrumental "
        "break, which avoids a visible blackout. Set 0 for the older behaviour that "
        "blanks the screen at once and fetches after. Default 1. Read by every swap "
        "site and the swap tick.",
    "swap_defer_instrumental_gap_s":
        "How long a stretch with no active lyric line counts as an instrumental "
        "break, which is treated as a safe moment to swap the lyric body. Raise it "
        "to demand a longer, more certain gap so a swap never interrupts singing. "
        "Lower it to take swap opportunities in shorter pauses. Typical 1 to 5 "
        "seconds, default 2.0. Read by the swap readiness check in both line and "
        "scrolling modes.",
    "swap_defer_max_s":
        "Safety cap on how long a queued lyric swap may wait for a tidy boundary "
        "before it is forced through anyway. Raise it to insist on a clean, "
        "unobtrusive swap even though the wrong words linger longer. Lower it to "
        "get the correct lyrics up sooner, accepting a mid-line snap. Typical 3 to "
        "15 seconds, default 8.0. Read when a swap is queued and by the swap commit "
        "tick.",
    "swap_defer_user_max_s":
        "The tighter version of the swap wait cap, used when YOU pressed Wrong "
        "lyrics rather than the app deciding by itself, on the basis that a "
        "deliberate press wants a fast fix. Raise it to favour a smoother swap over "
        "speed. Lower it to make the button feel more immediate. Typical 1 to 6 "
        "seconds, default 3.0. Read by the user-correction handler when it queues "
        "the swap.",
    "swap_fetch_hard_cap_s":
        "How long a queued replacement may wait for its fetch or generation to land "
        "before the app gives up, cancels the swap and, if it was a regeneration, "
        "falls back to generating from the audio. Raise it to let slow lookups of "
        "obscure songs still win. Lower it to stop wrong lyrics staying frozen on "
        "screen. Typical 25 to 90 seconds, default 40.0. Read by the swap commit "
        "tick.",
    "switch_needs_identity_evidence":
        "Whether switching or regenerating lyrics requires evidence that the song "
        "itself is wrong, not merely that it drifted out of time. Set 1 to enable, "
        "the default, so a correctly identified song that only lost sync gets "
        "resynced instead of thrown away and fetched again. Set 0 to disable and "
        "let poor sync alone trigger a swap. Default 1. Read by the lyric health "
        "verdict code in main.py.",
    "sync_apply_min_s":
        "The smallest correction, in seconds, that is actually applied in line "
        "mode. Anything below it is discarded as recognition wobble rather than "
        "real drift. Raise it to keep the lyrics still through minor noise, at the "
        "cost of leaving small errors uncorrected. Lower it to chase tighter timing "
        "with more frequent tiny moves. Typical 0.1 to 0.5, default 0.22. Read by "
        "the smooth offset gate.",
    "sync_apply_min_s_scroll":
        "The same minimum applied correction, but for scroll through belt layouts, "
        "where a small offset step visibly jerks the whole moving belt. Raise it to "
        "hold the belt steady through small automatic corrections, at the cost of "
        "accuracy. Lower it, or set 0 to match line mode, for tighter sync with "
        "more visible steps. Typical 0.2 to 1.0, default 0.25. Read by the smooth "
        "offset gate in scroll layouts.",
    "sync_confirm_hold_ms":
        "How long the app waits, in milliseconds, after holding a candidate offset "
        "before it takes the confirming listen. A longer wait separates the two "
        "listens by more song time, so two passes of the same chorus cannot falsely "
        "agree. Raise it for safer confirmation and slower locking. Lower it to "
        "lock sooner with more risk of a chorus mismatch. Typical 1500 to 4000, "
        "default 2600. Read by the sync confirm scheduler.",
    "sync_confirm_listen_s":
        "How many seconds of audio the confirming listen captures. A longer capture "
        "gives the recognizer more to work with, so the second read is more "
        "reliable, but it delays the decision and costs more CPU. Raise it when "
        "confirmations keep coming back unreadable. Lower it for a faster confirm "
        "on clean audio. Typical 3.0 to 8.0, default 5.0. Read by the sync confirm "
        "scheduler before starting the recognizer.",
    "sync_event_buffer_size":
        "How many recent sync diagnostic events are kept in memory for the "
        "developer console and diagnostics views. Older entries are dropped once "
        "the limit is reached. Raise it to keep a longer history for investigating "
        "an intermittent problem, using slightly more memory. Lower it to trim "
        "memory use. Typical 50 to 1000, default 200. Read by the sync event "
        "recorder when trimming the ring buffer.",
    "sync_event_enabled":
        "Whether sync diagnostic events are recorded at all. Set 1 to enable "
        "recording, which feeds the developer console and diagnostics with a trail "
        "of every correction, hold, and rejection, or 0 to disable and record "
        "nothing. The cost is a few appends per second at most, never per frame. "
        "Default 1. Read by the sync event recorder, which returns immediately when "
        "this is 0.",
    "sync_immediate_commit_s":
        "A correction larger than this many seconds is committed straight away "
        "instead of waiting for the current lyric line to finish, though it still "
        "glides visually. Raise it to defer more corrections to a line boundary, "
        "which is tidier but slower to fix a big error. Lower it to apply large "
        "fixes immediately. Typical 2.0 to 10.0, default 5.0. Read by the smooth "
        "offset path and the sync narrator.",
    "sync_live_follow_alpha":
        "How much of each newly heard offset is blended into the current one while "
        "following a live arrangement, on a scale of 0 to 1. The new target becomes "
        "the old value plus this fraction of the difference. Raise it toward 1 to "
        "follow live tempo changes quickly but jitter more. Lower it for heavy "
        "smoothing that lags real drift. Typical 0.15 to 0.6, default 0.35. Read by "
        "the live follow branch of the sync reader.",
    "sync_match_min":
        "How well the transcribed singing must match the lyric text, on a scale of "
        "0 to 1, before the app trusts a LARGE timing correction on a studio song. "
        "It guards against locking onto a repeated chorus somewhere else in the "
        "track. Raise it to demand a clearer match and reject more big jumps. Lower "
        "it to allow big corrections on weaker evidence. Typical 0.60 to 0.85, "
        "default 0.72. Read by the sync match threshold helper.",
    "sync_reject_strikes":
        "How many times in a row sync by ear may hear clear singing yet fail to "
        "anchor it anywhere in the loaded lyrics before the app concludes the "
        "cached lyrics are the wrong song and re-identifies. Raise it to be more "
        "forgiving of noisy or instrumental stretches. Lower it to reject a "
        "mismatched body faster, with more risk of a false rejection. Typical 2 to "
        "6, default 3. Read by the sync reject check.",
    "sync_tier_fast_s":
        "The escalated verification interval, in seconds, used while the app is "
        "still finding sync or has just seen a miss. It sets how quickly a new "
        "drift can even be noticed. Raise it to check less often and save CPU, "
        "which delays detection. Lower it to catch drift faster at the cost of more "
        "recognition work. Typical 8.0 to 25.0, default 12.0. Read by the sync tier "
        "cadence controller.",
    "sync_tier_listen_s":
        "How many seconds of audio each tier verification listen captures for "
        "transcription. A longer capture is more reliable on noisy or sparse vocals "
        "but slows the retry cycle and costs more CPU. Raise it when checks keep "
        "coming back inconclusive. Lower it for a faster retry cadence on clean "
        "audio. Typical 3.0 to 8.0, default 4.0. Read by the sync tier listen path "
        "and the fine tune listen path.",
    "sync_tier_mid_s":
        "The middle verification interval, one relaxation step above the fast tier, "
        "used after a single good check. It exists so the cadence does not jump "
        "straight from aggressive to relaxed. Raise it to relax faster after one "
        "success. Lower it to stay closer to the fast cadence while confidence "
        "builds. Typical 15 to 40, default 25.0. Read by the sync tier cadence "
        "controller and the idle sync heartbeat.",
    "sync_tier_ok_drift":
        "How small the measured timing error must be, in seconds, for a "
        "verification check to declare the song in sync and let the cadence relax. "
        "Raise it and the app accepts looser timing as good, correcting less but "
        "sitting further off. Lower it to demand tighter sync, which triggers more "
        "corrections. Typical 0.3 to 1.2, default 0.6. Read by the sync tier "
        "verdict handler.",
    "sync_tier_slow_s":
        "The relaxed verification interval used once sync has held steady, which is "
        "the cheapest steady state cadence. Raise it to spend less CPU on a track "
        "that is behaving, at the cost of a longer worst case delay before drift is "
        "noticed. Lower it to keep checking regularly even when things look fine. "
        "Typical 30 to 90, default 40.0. Read by the sync tier cadence controller.",
    "sync_win_ahead_s":
        "How far the highlight is allowed to run AHEAD of the singing before the "
        "app calls the song out of sync. Ahead is the forgiving direction for a "
        "listener, so this is the looser half of the window. Raise it to tolerate "
        "more early highlighting and correct less often. Lower it to demand tighter "
        "timing. Typical 0.10 to 0.30, default 0.17. Read by the perceptual in-sync "
        "test in the Shazam sync reader.",
    "sync_win_behind_s":
        "How far the highlight may lag BEHIND the singing before a correction "
        "fires. Late lyrics are the annoying direction, so this half of the window "
        "is deliberately tighter than the ahead half. Raise it to accept more "
        "lateness and correct less often. Lower it to react sooner, at the cost of "
        "more corrections. Typical 0.05 to 0.20, default 0.09. Read by the "
        "perceptual in-sync test in the Shazam sync reader.",
    "title_alias_album_fallback":
        "Handles the case where the listening service answers with an ALBUM name "
        "plus a long featured-artist list instead of the actual track name. Set 1 "
        "to accept the player's track title as correct and remember the album "
        "string as an alias, avoiding a pointless wrong-song teardown. Set 0 to "
        "treat the disagreement as an ordinary strike. Default 1, never applied "
        "inside concerts. Read by the listening-result handler.",
    "tpvr_gap_s":
        "The pause, in seconds, between the held first read and the confirming "
        "second read in the two point verification used by the sync tier on "
        "ordinary studio tracks. Because the second read is what releases the "
        "correction, this delay is effectively the lock latency. Raise it to "
        "separate the reads further and guard against repeated choruses. Lower it "
        "to lock sooner. Typical 1.0 to 3.0, default 1.5. Read by the sync tier "
        "listen path.",
    "translate_max_tries":
        "How many times the translation and romanization backfill may be kicked off "
        "for a single lyrics file before the app stops trying. Raise it to keep "
        "retrying through repeated network failures or translator rate limits. "
        "Lower it to give up sooner on lines that can never be satisfied, such as "
        "an English interjection inside a Japanese song. Typical 2 to 12, default "
        "6. Read by the translation kick.",
    "translate_recheck_s":
        "How often, while a song is loaded, the app re-examines whether every line "
        "has its English translation and romanization, so a failed or partial "
        "backfill heals mid-song instead of staying broken until the next track. "
        "Raise it to check less often and use less background work. Lower it to "
        "heal faster. Typical 10 to 120 seconds, default 30.0. Read by the main "
        "frame loop.",
    "unconfirmed_backoff_after_s":
        "How long a song must have been settled but unconfirmed before the slower "
        "unconfirmed polling interval starts being applied. It is the patience "
        "period before the app gives up on quick confirmation. Raise it to keep "
        "polling fast for longer on a stubborn track. Lower it to back off sooner "
        "and save CPU. Typical 10 to 60, default 25.0. Read by the recalibration "
        "scheduler.",
    "unconfirmed_backoff_s":
        "When a song has settled but simply cannot be confirmed by fingerprint, the "
        "recognition poll is slowed to at least this many seconds apart, which "
        "stops the app stuttering through endless failing listens. Raise it to back "
        "off harder and save CPU. Lower it to keep trying more often. Typical 15 to "
        "60, default 30.0. Read by the recalibration scheduler when choosing the "
        "next poll delay.",
    "uncorroborated_fast_after_s":
        "How long after a title lock the app waits before it starts using the "
        "reduced wrong song strike count on a lyric body that audio has never "
        "corroborated. The delay gives energy alignment a chance to prove a correct "
        "body first. Raise it to be more patient with slow corroboration. Lower it "
        "to drop a suspect body sooner. Typical 30 to 90, default 50.0. Read by the "
        "wrong song strike logic.",
    "verified_render_gate_s":
        "When song verification flips from confirmed to unconfirmed, the lyrics "
        "stay on screen for this many seconds before any teardown begins, so a "
        "brief disagreement does not blank the display. Raise it to hold lyrics "
        "through longer wobbles at the risk of showing a wrong body a little "
        "longer. Lower it to clear faster. Typical 1.0 to 10.0, default 3.0. Read "
        "by the verification gate in the Shazam result handler.",
    "whisper_child":
        "Whether the speech recognition engine runs in its own separate process. "
        "Set 1 to enable, the default, so a crash deep inside the graphics "
        "libraries kills only that helper rather than the whole app, and its load "
        "stays off the drawing thread. Set 0 to disable and run it inside the app, "
        "which is for diagnosis only. Default 1. Read by align.py through "
        "set_whisper_child.",
    "whisper_lang_lock":
        "Whether speech recognition is pinned to the song's known language instead "
        "of guessing chunk by chunk. Set 1 to enable, the default, which stops the "
        "engine inventing Japanese text over Spanish, English or Korean singing. "
        "Set 0 to disable and let it detect the language itself, which is useful "
        "for comparison testing. Default 1. Read by the deep transcription setup in "
        "main.py.",
    "whisper_vram_margin_mib":
        "How much spare graphics memory, in megabytes, a card must have beyond what "
        "the speech model itself needs before that card is used. Raise it to be "
        "more cautious, since running out of memory there kills the helper and "
        "drops the session to the slower processor path. Lower it to use the card "
        "in tighter conditions. Typical 256 to 2048, default 768. Read by align.py "
        "and passed to each helper process.",
    "window_titles_generic_browsers":
        "Whether ordinary browsers such as Chrome, Edge, Firefox, Opera, Brave, "
        "Vivaldi and Arc are also scanned for song titles. Set 1 to enable, useful "
        "if your browser is not reporting music properly. Set 0 to disable, the "
        "default, since browsers already report music to Windows and scanning every "
        "tab risks picking up unrelated pages. Default 0. Read by the window title "
        "fallback in main.py.",
    "window_titles_on":
        "Whether window titles from a small approved list of apps, such as the "
        "Steam overlay, Discord, Slack and Teams, are read to work out what is "
        "playing. Set 1 to enable, the default, which covers players that report "
        "nothing to Windows media. Set 0 to disable and never inspect window "
        "titles. Default 1. Read by the window title fallback in main.py and by "
        "window_titles.py.",
    "window_titles_poll_s":
        "How often, in seconds, the list of window titles is checked for a song. "
        "Raise it to check less frequently, which is fine for a background source "
        "and uses marginally less power. Lower it to notice a title change faster; "
        "the scan takes well under a millisecond, so short intervals are safe. "
        "Typical 0.5 to 10, default 2.0. Read by window_titles.py through the "
        "watcher started in main.py.",
    "wrong_immediate_clear":
        "What the Wrong lyrics tray button does to the words already on screen. Set "
        "1 to drop them instantly so the press visibly does something, then listen "
        "afresh. Set 0 for the older behaviour where the wrong lyrics linger up to "
        "a few seconds until a tidy line boundary. Automatic corrections keep the "
        "smooth deferred swap either way. Default 1. Read by the user-correction "
        "handler.",
    "wrong_song_strikes":
        "How many times in a row the app must hear the SAME other song before it "
        "accepts that the loaded lyrics are wrong and switches away from them. "
        "Raise it to stop one or two bad listens throwing away good lyrics. Lower "
        "it to react faster when the wrong song is on screen. Doubled automatically "
        "when the artist also disagrees. Typical 3 to 10, default 5. Read by the "
        "title-lock striking logic.",
    "wrong_song_uncorroborated_strikes":
        "The reduced strike count used when the loaded lyric body has never been "
        "confirmed against the audio and the initial alignment window has already "
        "passed. Raise it toward the normal strike count to be more forgiving of an "
        "unconfirmed but probably correct body. Lower it to tear down a bad cached "
        "body sooner. Typical 2 to 5, default 3. Read by the title-lock striking "
        "logic.",
    "wrong_streak_force_ai_gen_threshold":
        "How many Wrong lyrics presses inside the streak window it takes before the "
        "next attempt skips the normal lyric providers entirely and generates "
        "lyrics from the audio. Raise it to give the providers more chances first. "
        "Lower it to reach generation almost at once, useful when no provider "
        "carries the song. Typical 1 to 4, default 2. Read by the wrong-streak "
        "counter.",
    "wrong_streak_window_s":
        "How close together your Wrong lyrics presses must be to count as one "
        "streak rather than unrelated one-off corrections. Raise it so presses "
        "spread over a longer period still add up and escalate to generated lyrics. "
        "Lower it so only rapidly repeated presses escalate. Typical 30 to 180 "
        "seconds, default 60.0. Read by the wrong-streak counter in the user- "
        "correction path.",
    "yt_description_cache_days":
        "Intended as how many days a fetched video description stays remembered "
        "before it is looked up again. Raising or lowering it currently changes "
        "nothing, because no code reads this key: the real cache is a fixed "
        "64-entry in-process list with no expiry that empties when the app closes. "
        "Default 30. Declared in the tune table only; the cache itself lives in "
        "yt_description.py.",
    "yt_description_lookup":
        "Whether the app reads a video description to harvest credits such as "
        "composer, lyricist and vocalist, which then feed extra artist guesses into "
        "the lyric search. Set 1 to enable it, giving an ambiguous title a second "
        "signal to search on, 0 to disable it so only the player title and artist "
        "are used. Default 1. Read by the description-metadata fetcher on browser "
        "sources.",
    "yt_description_timeout_s":
        "How long the metadata-only description lookup may take before it is "
        "abandoned. Raise it to give a slow connection more chance to return the "
        "credits that help disambiguate a very common title. Lower it to fail fast "
        "and stop a background worker lingering on a stalled network call. Typical "
        "3 to 20 seconds, default 8.0. Passed as the yt-dlp socket timeout.",
}
