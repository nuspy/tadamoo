import 'package:flutter/foundation.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:speech_to_text/speech_to_text.dart';

import 'i18n.dart';
import 'settings.dart';

/// Reading answers aloud and writing what is said, with what the phone already has: its own voices and its own speech recognition.
class Voice extends ChangeNotifier {
  Voice(this.settings);
  final AppSettings settings;
  final FlutterTts _tts = FlutterTts();
  final SpeechToText _stt = SpeechToText();
  bool _sttReady = false;
  bool speaking = false;
  bool listening = false;
  String? problem;

  String get _locale {
    if (settings.speechLocale.isNotEmpty) return settings.speechLocale;
    return Texts.language == 'it' ? 'it-IT' : 'en-GB';
  }

  Future<void> init() async {
    _tts.setCompletionHandler(() {
      speaking = false;
      notifyListeners();
    });
    _tts.setCancelHandler(() {
      speaking = false;
      notifyListeners();
    });
    _tts.setErrorHandler((_) {
      speaking = false;
      notifyListeners();
    });
  }

  /// The text of an answer without the marks of Markdown and the links to files, for the voice.
  static String plain(String markdown) {
    var t = markdown;
    t = t.replaceAll(RegExp(r'```[\s\S]*?```'), ' ');
    t = t.replaceAllMapped(RegExp(r'!?\[([^\]]*)\]\([^)]*\)'), (m) => m.group(1) ?? '');
    t = t.replaceAll(RegExp(r'[`*_#>|]+'), ' ');
    return t.replaceAll(RegExp(r'\s+'), ' ').trim();
  }

  Future<void> speak(String markdown) async {
    final text = plain(markdown);
    if (text.isEmpty) return;
    try {
      await _tts.setLanguage(_locale);
      speaking = true;
      notifyListeners();
      await _tts.speak(text);
    } catch (e) {
      speaking = false;
      problem = '$e';
      notifyListeners();
    }
  }

  Future<void> stopSpeaking() async {
    await _tts.stop();
    speaking = false;
    notifyListeners();
  }

  /// Starts writing what is said; `onText` gets the words so far and whether the phone considers them final.
  Future<bool> listen(void Function(String text, bool done) onText) async {
    try {
      if (!_sttReady) {
        _sttReady = await _stt.initialize(onStatus: (s) {
          if (s == 'done' || s == 'notListening') {
            listening = false;
            notifyListeners();
          }
        }, onError: (e) {
          listening = false;
          problem = e.errorMsg;
          notifyListeners();
        });
      }
      if (!_sttReady) return false;
      await _stt.listen(onResult: (r) => onText(r.recognizedWords, r.finalResult), listenOptions: SpeechListenOptions(partialResults: true, cancelOnError: true, localeId: _locale.replaceAll('-', '_')));
      listening = true;
      notifyListeners();
      return true;
    } catch (e) {
      problem = '$e';
      listening = false;
      notifyListeners();
      return false;
    }
  }

  Future<void> stopListening() async {
    await _stt.stop();
    listening = false;
    notifyListeners();
  }
}
