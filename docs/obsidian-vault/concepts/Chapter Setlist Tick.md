# Chapter Setlist Tick (`_concert_setlist_tick`)

On entering a chapter: skip non-song titles and **talk-dominated chapters** (`mc_frac`),
honour a recent sound lock (and **re-run the chapter when it ages out** — Bug E),
anchor `offset = -onset` from the [[Offline Concert Analysis]] (or the chapter start
under a between-songs hold), then load cached / fetch distinctive / wait on Shazam for
generic titles; a generation deadline backstops unfetchable originals.
