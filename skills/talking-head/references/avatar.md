# The talking head (LongCat-Video-Avatar 1.5)

`tadamoo.media.limits.avatar_request(image, audio, prompt, resolution, seed=..., negative=...)` builds the request, `tadamoo.media.client.run("avatar", request, output, settings, url=..., held=True)`
runs it. LTX Studio's `LongCatAvatar` environment runs the model (int8), 480p by default.

- **Length.** The video lasts as long as the audio: the model makes a first clip of 93 frames at 25 fps (3.72 s) and each continuation reuses the last 13 frames as its condition and adds 80 (3.2 s).
  The continuation is internal, so one audio of 40 seconds gives one continuous take with no seam.
- **Knobs.** The distilled model (8 steps, text and audio guidance fixed at 1) leaves the prompt, the negative prompt, the still and the audio. The audio is the strongest: a louder, sharper
  recording opens the mouth wider and moves hands and head more.
- **What helped (measured against a reference video).** A tamed copy of the audio (high-pass 90 Hz, low-pass 4.2 kHz, compressor ratio 3, -9 dB), a prompt that asks for a composed, calm presenter with
  a small mouth opening, hands folded out of the picture, a negative prompt against wide mouth, gestures, nodding and swaying, and a still framed from the chest up so that no hand is in the picture.
- **Cost.** About 65 s of GPU for each second of speech.
- **Verification.** Measure the lip aperture (inner lips over face height), the head speed and the shoulder speed on the result and on a reference with `tadamoo/videoedit/qa.py`
  (`python -m tadamoo.videoedit.qa VIDEO --body`); aim at the reference's percentiles.
