# TaDaMoo for Android

A phone version of the TaDaMoo conversation page, built with Flutter for Android (the main target is the Samsung S25 Ultra). It talks to the TaDaMoo server on your PC through **Tailscale**; nothing is opened to the internet.

## What it does

- **Chat** and **Agent**: the same conversations as on the PC. Agent works in the folders you added on the PC. Questions come with their buttons, the four modes change with the chip at the top, answers can be copied, read aloud, and branched from.
- **New conversation**: a sheet asks for the mode, the reasoning effort, the allow rules and (in a folder) the working copy, the fields the PC's page asks for. The model is the one loaded on the PC. In Agent, a button adds a folder by its path on the PC and another takes it off the list.
- **Settings > More**: *Personal skills* (list, read the text, make one, import a `.pskill` file, run it in a new conversation, delete) and *MCP servers and requests* (switch the agent's MCP servers and skills on and off, approve one candidate of an install request or reject it). A blocked conversation always offers *Resume*.
- **Conversation options** (the tune button in a conversation, and in Settings): *Prompt analysis* (off, rules, full), *Optimize the prompt* (only with the technical session), *Work until done*, the state of the technical session (read only), and in Agent the role (Developer, Plan, Ask, Debug, Orchestrate, General). The three switches are the PC's and are read from and saved on the PC; the PC must allow a phone to change them.
- **In the conversation**: background commands with their output, the technical session's approvals, the course and completion checks, interrupted batches, and the question about orders found in outside text or asked by a hook.
- **No time limit**: requests, uploads and the following of a conversation wait as long as the connection stays up.
- **Files in answers**: a link to a file the agent made, or to a file in the folder, is a button that saves it with the phone's "Save as" window. Pictures the agent kept are shown; diagrams show their source.
- **The + button**: *Image* (the phone's photo picker), *Photo* (the Camera app), *Documents/Files*: *Local* (the phone's document window), *Server* (the same window, opened on "TaDaMoo - PC", the folders added on the PC, then the drives) and *Network* (the devices Tailscale sees, or an address you type; signs in over SMB and lets you mark files).
- **Voice**: the microphone writes what you say with the phone's own speech recognition; answers can be read aloud with its own voice, one by one or automatically. The language follows the phone unless you set it.
- **Avatar**: talk with the PC's voice. Hold the bar and speak: the PC's own speech recognition, brain and speech synthesis answer. Above the bar: *lips* (Direct, the loaded model) and *brain* (Harness, the PC's agent; its permission questions stay on the screen), then where the sound goes: *Bluetooth headset*, *speakerphone*, *earpiece with the phone's microphone*. The *Text chat* accordion (closed at first) holds the conversation and the field to type in; while it is open the avatar is smaller. A small face button turns the avatar off (voice only). It reconnects by itself and uses the same address and key as the rest of the app, through the server (`GET /api/voice/gateway/ws`); see `docs/voice/gateway.md`.
- **The avatar is drawn on the phone**: *Visemes* (the photo with 15 mouth drawings that follow the PC's timeline, on the clock of the voice being played; no model) and *Wav2Lip* (an INT8 ONNX model run with ONNX Runtime on the straightened 96 px face). The PC's video is only the emergency, when nothing else can be drawn. The app downloads the avatar's files (`manifest.json`, `assets.zip`) through the server and keeps only files whose SHA-256 matches the manifest. It tells the PC in its first message what it can draw (`render_modes`, with `engine` and `resolution`): `visemes` only when those files are on the phone, `wav2lip` only when the model (36.7 MB, downloaded on request with resume) is verified and the *Avatar test* passed on this phone, and `video` always last. A mode that fails (too slow, too hot, the runtime fails) is reported with `render_failed` and the PC moves to the next. Declaring something new means opening the connection again, which the app does by itself. Visemes is the default engine.
- **Settings > Avatar and voice**: avatar on or off; the *animation system* (Visemes, Wav2Lip, Ultralight, FeatherTalk, Video from the PC) with each engine's weight and an honest line about what it needs on this phone (the files, the model with its Download button, the test) and buttons to get it; Ultralight and FeatherTalk are shown as not available in this build; the picture's *resolution* (96 to 480 px) and *quality* (low, medium, high), sent to the PC; the *personality* and Direct or Harness by default, sent to the PC when the tab is connected; the audio route and *Full duplex on the speakerphone*; the *Audio test* and the *Avatar test* (the model runs, its output resembles its input, frames per second and ms per frame on this phone), both as text to copy and send. All of it is kept on the phone. The tab says by which engine and at what size the PC says the avatar is drawn.
- **Speaking over the avatar**: with a Bluetooth headset (or the speakerphone when the audio test found an echo canceller and full duplex is on) the microphone stays open while the avatar speaks. A local voice detector lowers the avatar's voice to 30 percent at once and tells the PC (`user_speaking`); the PC decides to ignore, finish first or stop, and may offer to resume (a line with a Continue button). Otherwise the microphone is closed while the avatar speaks (half duplex). What the phone does and does not prove is in `docs/voice/phone-audio.md`; the messages are in `docs/voice/gateway.md` (Interruptions).
- **Cron Jobs**: switch the jobs on and off, start one now and dismiss the requests waiting for you. Jobs are written on the PC.
- **Settings**: the server's address (IP:port) and this phone's key, a QR scan, a test of the link.

## Set up

1. On the PC, in the TaDaMoo app: *Settings > Phones*. Install and connect Tailscale there, turn phones on, give the phone a name and press *Make a key*. A QR code appears.
2. On the phone: install **Tailscale** from the Play Store and sign in with the same account as on the PC.
3. Install `TaDaMoo-mobile.apk` on the phone (allow installing from this source when Android asks), open it, go to *Settings*, press *Scan the QR code*. The address and the key are filled in and the link is tested. You can also type them.

The key is the phone's own: take it away on the PC and the phone stops working at once. A phone can use the model, the conversations, the files of the PC, the routines and the plans, and works in any folder; what answers only to the PC itself (the wallet, the vault, the settings, the loading of models) stays there. A phone with a key also uses the personal skills and answers install requests; the rest of the capabilities stays on the PC.

## Build

The Avatar tab needs three plugins (`record`, `flutter_pcm_sound`, `flutter_webrtc`); `flutter pub get` fetches them, and `lib/src/avatar_platform.dart` is the only file that uses them. Android needs `minSdk` 23 or more for WebRTC. The audio routing, the audio test and the player of the PC's voice are Kotlin (`AudioRouteChannel.kt`, `CommPlayer.kt`, channel `app.basalt/audio`); Bluetooth adds the `BLUETOOTH_CONNECT` permission, asked for only when its icon is tapped. Wav2Lip is Kotlin too (`Wav2LipEngine.kt`, `Wav2LipChannel.kt`) with the Gradle dependency `com.microsoft.onnxruntime:onnxruntime-android:1.22.0`. Backups are off (`allowBackup=false`, and the rules exclude the preferences, which hold the PC's address and this phone's key).

```
cd mobile
flutter pub get
flutter build apk --release        # build/app/outputs/flutter-apk/app-release.apk
```

The release build is signed with the debug key, which is fine for installing it by hand on your own phone; sign it with your own key before giving it to others. The Android SDK and the Flutter SDK are needed.

## Tests

```
python mobile/tool/mock_server.py      # prints PORT and FOLDER; leave it running
flutter test --dart-define=BASALT_TEST_URL=http://127.0.0.1:<PORT> --dart-define=BASALT_TEST_FOLDER=<FOLDER>
```

`gateway_client_test.dart`, `avatar_controller_test.dart`, `avatar_screen_test.dart` and `avatar_barge_test.dart` check the Avatar tab against a fake gateway, fake devices and a fake PC (no plugin needed); `avatar_settings_test.dart` checks the Settings section and what reaches the PC; `audio_route_test.dart` and `audio_player_channel_test.dart` check the audio routing against a fake platform channel; `viseme_timeline_test.dart`, `viseme_renderer_test.dart`, `wav2lip_test.dart`, `avatar_assets_test.dart`, `avatar_hub_test.dart`, `avatar_render_test.dart` and `avatar_engines_ui_test.dart` check the on-device avatar (timelines on the audio clock, files and SHA-256, the model download, the fallback chain, what is declared and when) against fakes; `new_chat_test.dart` checks what a new conversation sends; `protocol_test.dart` runs the app's side of the protocol against a real server with a scripted model; `answer_view_test.dart` checks how an answer is drawn. Without `BASALT_TEST_URL` the protocol tests are skipped.

## Notes

- The link is plain HTTP on the Tailscale address (Tailscale encrypts it); Android is told so in the manifest.
- *Server* files come from `GET /api/files/roots|list|read`, offered to the phone's document window by `BasaltDocumentsProvider.kt`, which reads the server address and key the app keeps.
- *Network* uses a pure Dart SMB client (SMB 1, 2.0 and 2.1); a device that only speaks SMB 3 will not answer.
