# Basalt for Android

A phone version of the Basalt conversation page, built with Flutter for Android (the main target is the Samsung S25 Ultra). It talks to the Basalt server on your PC through **Tailscale**; nothing is opened to the internet.

## What it does

- **Chat** and **Agent**: the same conversations as on the PC. Agent works in the folders you added on the PC. Questions come with their buttons, the four modes change with the chip at the top, answers can be copied, read aloud, and branched from.
- **New conversation**: a sheet asks for the mode, the reasoning effort, the allow rules and (in a folder) the working copy, the fields the PC's page asks for. The model is the one loaded on the PC. In Agent, a button adds a folder by its path on the PC and another takes it off the list.
- **Settings > More**: *Personal skills* (list, read the text, make one, import a `.pskill` file, run it in a new conversation, delete) and *MCP servers and requests* (switch the agent's MCP servers and skills on and off, approve one candidate of an install request or reject it). A blocked conversation always offers *Resume*.
- **Conversation options** (the tune button in a conversation, and in Settings): *Prompt analysis* (off, rules, full), *Optimize the prompt* (only with the technical session), *Work until done*, the state of the technical session (read only), and in Agent the role (Developer, Plan, Ask, Debug, Orchestrate, General). The three switches are the PC's and are read from and saved on the PC; the PC must allow a phone to change them.
- **In the conversation**: background commands with their output, the technical session's approvals, the course and completion checks, interrupted batches, and the question about orders found in outside text or asked by a hook.
- **No time limit**: requests, uploads and the following of a conversation wait as long as the connection stays up.
- **Files in answers**: a link to a file the agent made, or to a file in the folder, is a button that saves it with the phone's "Save as" window. Pictures the agent kept are shown; diagrams show their source.
- **The + button**: *Image* (the phone's photo picker), *Photo* (the Camera app), *Documents/Files*: *Local* (the phone's document window), *Server* (the same window, opened on "Basalt - PC", the folders added on the PC, then the drives) and *Network* (the devices Tailscale sees, or an address you type; signs in over SMB and lets you mark files).
- **Voice**: the microphone writes what you say with the phone's own speech recognition; answers can be read aloud with its own voice, one by one or automatically. The language follows the phone unless you set it.
- **Cron Jobs**: switch the jobs on and off, start one now and dismiss the requests waiting for you. Jobs are written on the PC.
- **Settings**: the server's address (IP:port) and this phone's key, a QR scan, a test of the link.

## Set up

1. On the PC, in the Basalt app: *Settings > Phones*. Install and connect Tailscale there, turn phones on, give the phone a name and press *Make a key*. A QR code appears.
2. On the phone: install **Tailscale** from the Play Store and sign in with the same account as on the PC.
3. Install `Basalt-mobile.apk` on the phone (allow installing from this source when Android asks), open it, go to *Settings*, press *Scan the QR code*. The address and the key are filled in and the link is tested. You can also type them.

The key is the phone's own: take it away on the PC and the phone stops working at once. A phone can use the model, the conversations, the files of the PC, the routines and the plans, and works in any folder; what answers only to the PC itself (the wallet, the vault, the settings, the loading of models) stays there. A phone with a key also uses the personal skills and answers install requests; the rest of the capabilities stays on the PC.

## Build

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

`new_chat_test.dart` checks what a new conversation sends; `protocol_test.dart` runs the app's side of the protocol against a real server with a scripted model; `answer_view_test.dart` checks how an answer is drawn. Without `BASALT_TEST_URL` the protocol tests are skipped.

## Notes

- The link is plain HTTP on the Tailscale address (Tailscale encrypts it); Android is told so in the manifest.
- *Server* files come from `GET /api/files/roots|list|read`, offered to the phone's document window by `BasaltDocumentsProvider.kt`, which reads the server address and key the app keeps.
- *Network* uses a pure Dart SMB client (SMB 1, 2.0 and 2.1); a device that only speaks SMB 3 will not answer.
