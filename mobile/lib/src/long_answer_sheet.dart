import 'package:flutter/material.dart';

import 'i18n.dart';
import 'long_answer.dart';
import 'settings.dart';

/// The Long answer dialog: what to write, how long, in which language. Returns how it stays armed: 'once' (the next message only),
/// 'on' (the whole conversation) or 'off', or null when it is closed without changing anything. The choices are kept on the phone.
Future<String?> showLongAnswerSheet(BuildContext context, AppSettings settings, {required bool armed}) {
  return showModalBottomSheet<String>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (c) => Padding(padding: EdgeInsets.only(bottom: MediaQuery.of(c).viewInsets.bottom), child: LongAnswerForm(settings: settings, armed: armed)),
  );
}

class LongAnswerForm extends StatefulWidget {
  const LongAnswerForm({super.key, required this.settings, required this.armed});
  final AppSettings settings;
  final bool armed;
  @override
  State<LongAnswerForm> createState() => _LongAnswerFormState();
}

class _LongAnswerFormState extends State<LongAnswerForm> {
  late LongAnswerOptions o = widget.settings.longAnswer;
  late final words = TextEditingController(text: '${o.words}');
  late final lang = TextEditingController(text: o.lang);
  late final instr = TextEditingController(text: o.instr);
  late final style = TextEditingController(text: o.style);
  String problem = '';

  @override
  void dispose() {
    words.dispose();
    lang.dispose();
    instr.dispose();
    style.dispose();
    super.dispose();
  }

  LongAnswerOptions get _now => o.copy(words: int.tryParse(words.text.trim()) ?? 0, lang: lang.text, instr: instr.text, style: style.text);

  void _confirm(String how) {
    final v = _now;
    final bad = v.problem;
    if (bad.isNotEmpty) {
      setState(() => problem = bad);
      return;
    }
    widget.settings.setLongAnswer(v);
    Navigator.pop(context, how);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
        Text(Texts.t('la.title'), style: theme.textTheme.titleMedium),
        const SizedBox(height: 6),
        Text(Texts.t('la.help'), style: theme.textTheme.bodySmall),
        const SizedBox(height: 14),
        DropdownButtonFormField<String>(
          key: ValueKey('la-mode-${o.mode}'),
          initialValue: o.mode,
          isExpanded: true,
          decoration: InputDecoration(labelText: Texts.t('la.mode'), helperText: Texts.t('la.mode.h'), helperMaxLines: 3, border: const OutlineInputBorder()),
          items: [for (final m in LongAnswerOptions.modes) DropdownMenuItem(value: m, child: Text(Texts.t('la.mode.$m'), overflow: TextOverflow.ellipsis))],
          onChanged: (v) => setState(() => o = _now.copy(mode: v ?? o.mode)),
        ),
        if (o.usesSize) ...[
          const SizedBox(height: 14),
          DropdownButtonFormField<String>(
            key: ValueKey('la-size-${o.size}'),
            initialValue: o.size,
            isExpanded: true,
            decoration: InputDecoration(labelText: Texts.t('la.size'), helperText: Texts.t('la.size.h'), helperMaxLines: 4, border: const OutlineInputBorder()),
            items: [for (final s in LongAnswerOptions.sizes) DropdownMenuItem(value: s, child: Text(Texts.t('la.size.$s'), overflow: TextOverflow.ellipsis))],
            onChanged: (v) => setState(() => o = _now.copy(size: v ?? o.size)),
          ),
        ],
        if (o.usesWords) ...[
          const SizedBox(height: 14),
          TextField(
            controller: words,
            keyboardType: TextInputType.number,
            decoration: InputDecoration(labelText: Texts.t(o.size == 'hybrid' ? 'la.words.base' : 'la.words'), helperText: Texts.t('la.words.h'), helperMaxLines: 3, border: const OutlineInputBorder()),
          ),
        ],
        const SizedBox(height: 14),
        TextField(
          controller: lang,
          decoration: InputDecoration(labelText: Texts.t(o.mode == 'translate' ? 'la.lang.to' : 'la.lang'), hintText: Texts.t('la.lang.ph'), helperText: Texts.t('la.lang.h'), helperMaxLines: 2, border: const OutlineInputBorder()),
        ),
        if (o.mode == 'rewrite') ...[
          const SizedBox(height: 14),
          TextField(
            controller: instr,
            decoration: InputDecoration(labelText: Texts.t('la.instr'), hintText: Texts.t('la.instr.ph'), helperText: Texts.t('la.instr.h'), helperMaxLines: 2, border: const OutlineInputBorder()),
          ),
        ],
        const SizedBox(height: 14),
        TextField(
          controller: style,
          decoration: InputDecoration(labelText: Texts.t('la.style'), helperText: Texts.t('la.style.h'), helperMaxLines: 2, border: const OutlineInputBorder()),
        ),
        if (problem.isNotEmpty) Semantics(liveRegion: true, child: Padding(padding: const EdgeInsets.only(top: 10), child: Text(problem, style: TextStyle(color: theme.colorScheme.error)))),
        const SizedBox(height: 16),
        FilledButton(style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)), onPressed: () => _confirm('once'), child: Text(Texts.t('la.once'))),
        const SizedBox(height: 8),
        OutlinedButton(style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(48)), onPressed: () => _confirm('on'), child: Text(Texts.t('la.on'))),
        const SizedBox(height: 8),
        Row(children: [
          if (widget.armed) Expanded(child: TextButton(style: TextButton.styleFrom(minimumSize: const Size.fromHeight(48)), onPressed: () => Navigator.pop(context, 'off'), child: Text(Texts.t('la.off')))),
          Expanded(child: TextButton(style: TextButton.styleFrom(minimumSize: const Size.fromHeight(48)), onPressed: () => Navigator.pop(context), child: Text(Texts.t('la.close')))),
        ]),
      ]),
    );
  }
}
