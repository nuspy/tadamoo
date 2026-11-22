import 'dart:convert';

import 'i18n.dart';

/// What the person asks of a long answer: the text is written in parts of about 2000 words, each planned and checked on its own, so it is not
/// cut short. The same choices as the PC page's "Long answer" button. In the conversation with the agent the message ends with one line that
/// the harness reads (`[long answer: mode=write; size=fixed; words=8000]`) and acts on at once.
class LongAnswerOptions {
  const LongAnswerOptions({this.mode = 'write', this.size = 'fixed', this.words = 3000, this.lang = '', this.instr = '', this.style = ''});

  /// write (a new text from the message), translate (the message is the text) or rewrite (the message is the text and the task says how).
  final String mode;

  /// For a new text: fixed (the words given), heuristic (the agent judges) or hybrid (the words given are a base for its judgment).
  final String size;
  final int words;

  /// The language of the text, or the language to translate into.
  final String lang;

  /// What to change, for a rewrite.
  final String instr;
  final String style;

  static const modes = ['write', 'translate', 'rewrite'];
  static const sizes = ['fixed', 'heuristic', 'hybrid'];
  static const minWords = 50;
  static const maxWords = 200000;

  LongAnswerOptions copy({String? mode, String? size, int? words, String? lang, String? instr, String? style}) =>
      LongAnswerOptions(mode: mode ?? this.mode, size: size ?? this.size, words: words ?? this.words, lang: lang ?? this.lang, instr: instr ?? this.instr, style: style ?? this.style);

  /// Whether the length field is used: a translation or a rewrite follows its source, and a judged length has no number.
  bool get usesWords => mode == 'write' && size != 'heuristic';
  bool get usesSize => mode == 'write';

  /// What is missing before it can be armed, in the app's language ('' when nothing is).
  String get problem {
    if (usesWords && (words < minWords || words > maxWords)) return Texts.t('la.bad.words');
    if (mode == 'translate' && lang.trim().isEmpty) return Texts.t('la.bad.lang');
    if (mode == 'rewrite' && instr.trim().isEmpty) return Texts.t('la.bad.instr');
    return '';
  }

  /// A value as one short run of words: the characters that would end the line or the field are replaced.
  static String _part(String s) {
    final t = s.replaceAll(RegExp(r'[\]\n;]+'), ' ').trim();
    return t.length > 80 ? t.substring(0, 80) : t;
  }

  /// The line the harness reads at the end of the message.
  String get line {
    final bits = [
      'mode=$mode',
      if (mode == 'write') ...['size=$size', if (size != 'heuristic') 'words=$words'],
      if (lang.trim().isNotEmpty) 'language=${_part(lang)}',
      if (mode == 'rewrite' && instr.trim().isNotEmpty) 'task=${_part(instr)}',
      if (style.trim().isNotEmpty) 'style=${_part(style)}',
    ];
    return '[long answer: ${bits.join('; ')}]';
  }

  /// The message with the line after it.
  String decorate(String text) => '$text\n\n$line';

  Map<String, dynamic> toJson() => {'mode': mode, 'size': size, 'words': words, 'lang': lang, 'instr': instr, 'style': style};

  factory LongAnswerOptions.fromJson(Object? raw) {
    if (raw is! Map) return const LongAnswerOptions();
    final mode = '${raw['mode']}', size = '${raw['size']}';
    return LongAnswerOptions(
      mode: modes.contains(mode) ? mode : 'write',
      size: sizes.contains(size) ? size : 'fixed',
      words: raw['words'] is num ? (raw['words'] as num).toInt() : 3000,
      lang: '${raw['lang'] ?? ''}',
      instr: '${raw['instr'] ?? ''}',
      style: '${raw['style'] ?? ''}',
    );
  }

  static LongAnswerOptions decode(String text) {
    if (text.isEmpty) return const LongAnswerOptions();
    try {
      return LongAnswerOptions.fromJson(jsonDecode(text));
    } catch (_) {
      return const LongAnswerOptions();
    }
  }
}

/// The most conversations whose "stays on" is remembered; the oldest go first.
const maxLongOnKept = 200;
