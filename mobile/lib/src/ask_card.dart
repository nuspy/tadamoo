import 'dart:convert';

import 'package:flutter/material.dart';

import 'i18n.dart';

/// What the person has chosen on a card of questions (single choice, multiple choice, free text, yes/no), as the page keeps it: option ids
/// and typed text. The answer leaves as `ask:` + JSON; the server checks it against the card it stored and puts it in words for the agent.
class AskSelection {
  AskSelection(this.questions) : _sel = {for (final q in questions) '${q['id']}': _One()};

  static const prefix = 'ask:';
  static const maxText = 500;
  final List<Map<String, dynamic>> questions;
  final Map<String, _One> _sel;

  Map<String, dynamic>? _q(String qid) {
    for (final q in questions) {
      if ('${q['id']}' == qid) return q;
    }
    return null;
  }

  List<String> picked(String qid) => List.unmodifiable(_sel[qid]?.picked ?? const <String>[]);
  bool other(String qid) => _sel[qid]?.other ?? false;
  String text(String qid) => _sel[qid]?.text ?? '';

  void setPick(String qid, String oid, bool on) {
    final q = _q(qid), s = _sel[qid];
    if (q == null || s == null) return;
    if (q['kind'] == 'multi') {
      if (!on) {
        s.picked.remove(oid);
      } else if (!s.picked.contains(oid)) {
        s.picked.add(oid);
      }
    } else {
      s.picked
        ..clear()
        ..addAll(on ? [oid] : const []);
      s.other = false;
    }
  }

  void setOther(String qid, bool on) {
    final q = _q(qid), s = _sel[qid];
    if (q == null || s == null) return;
    s.other = on;
    if (on && q['kind'] == 'single') s.picked.clear();
    if (!on) s.text = '';
  }

  void setText(String qid, String value) {
    final q = _q(qid), s = _sel[qid];
    if (q == null || s == null) return;
    s.text = value.length > maxText ? value.substring(0, maxText) : value;
    if (q['kind'] != 'free') {
      s.other = s.text.isNotEmpty || s.other;
      if (s.text.isNotEmpty && q['kind'] == 'single') s.picked.clear();
    }
  }

  String _typed(Map<String, dynamic> q, _One s) => (q['kind'] == 'free' || s.other) ? s.text.trim() : '';

  /// The first question that still needs an answer, or null.
  String? missing() {
    for (final q in questions) {
      final s = _sel['${q['id']}']!;
      if (q['required'] != false && s.picked.isEmpty && _typed(q, s).isEmpty) return '${q['id']}';
    }
    return null;
  }

  String encode(String status) {
    final answers = <String, dynamic>{};
    if (status == 'submitted') {
      for (final q in questions) {
        final s = _sel['${q['id']}']!;
        answers['${q['id']}'] = {'picked': List<String>.from(s.picked), 'text': _typed(q, s)};
      }
    }
    return prefix + jsonEncode({'v': 1, 'status': status, 'answers': answers});
  }

  /// A card with one question of single or confirm kind is answered by one tap.
  bool get fast => questions.length == 1 && (questions.first['kind'] == 'single' || questions.first['kind'] == 'confirm');

  /// One line for each question, for the read-only card after the answer.
  List<String> lines() {
    return [
      for (final q in questions)
        () {
          final s = _sel['${q['id']}']!;
          final byId = {for (final o in (q['options'] as List? ?? const [])) '${(o as Map)['id']}': '${o['label']}'};
          final parts = [for (final p in s.picked) byId[p] ?? p];
          final t = _typed(q, s);
          if (t.isNotEmpty) parts.add(q['kind'] == 'free' ? t : '“$t”');
          return '${q['text']} → ${parts.isEmpty ? Texts.t('ask.noanswer') : parts.join('; ')}';
        }(),
    ];
  }
}

class _One {
  final List<String> picked = [];
  bool other = false;
  String text = '';
}

/// The card: rows of at least 48 dp, the recommended option tagged in words, a row of their own, Send / Skip / answer in the chat.
class QuestionsCard extends StatefulWidget {
  const QuestionsCard({super.key, required this.questions, required this.intro, required this.answered, required this.onSend});

  final List<Map<String, dynamic>> questions;
  final String intro;

  /// null while the card waits; otherwise how it was closed: submitted, skipped, chat or '' (the person wrote in the chat).
  final String? answered;
  final void Function(String value, String how) onSend;

  @override
  State<QuestionsCard> createState() => _QuestionsCardState();
}

class _QuestionsCardState extends State<QuestionsCard> {
  late final AskSelection sel = AskSelection(widget.questions);
  final Map<String, TextEditingController> typed = {};

  TextEditingController _ctl(String qid) => typed.putIfAbsent(qid, () => TextEditingController(text: sel.text(qid)));

  @override
  void dispose() {
    for (final c in typed.values) {
      c.dispose();
    }
    super.dispose();
  }

  void _submit(String how) {
    if (how == 'submitted' && sel.missing() != null) return;
    widget.onSend(sel.encode(how), how);
  }

  Widget _row({required bool multi, required bool checked, required String label, String description = '', bool recommended = false, required ValueChanged<bool> onChanged}) {
    final theme = Theme.of(context);
    return Semantics(
      checked: checked,
      inMutuallyExclusiveGroup: !multi,
      child: InkWell(
        borderRadius: BorderRadius.circular(8),
        onTap: () => onChanged(multi ? !checked : true),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 48),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 6),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Padding(
                padding: const EdgeInsets.only(top: 2, right: 10),
                child: Icon(multi ? (checked ? Icons.check_box : Icons.check_box_outline_blank) : (checked ? Icons.radio_button_checked : Icons.radio_button_unchecked),
                    color: checked ? theme.colorScheme.primary : null),
              ),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Wrap(crossAxisAlignment: WrapCrossAlignment.center, spacing: 8, children: [
                    Text(label, style: const TextStyle(fontWeight: FontWeight.w600)),
                    if (recommended) Chip(visualDensity: VisualDensity.compact, avatar: const Icon(Icons.star, size: 16), label: Text(Texts.t('ask.recommended'))),
                  ]),
                  if (description.isNotEmpty) Text(description, style: theme.textTheme.bodySmall),
                ]),
              ),
            ]),
          ),
        ),
      ),
    );
  }

  Widget _question(Map<String, dynamic> q, int index) {
    final qid = '${q['id']}', kind = '${q['kind']}';
    final multi = kind == 'multi';
    final total = widget.questions.length;
    final head = [if (total > 1) '${Texts.t('ask.n_of_m', [index + 1, total])}  ', '${q['text']}', if (q['required'] == false) '  ${Texts.t('ask.optional')}'].join();
    final kids = <Widget>[Semantics(header: true, child: Text(head, style: const TextStyle(fontWeight: FontWeight.w600)))];
    if (kind == 'free') {
      kids.add(Padding(
        padding: const EdgeInsets.only(top: 6),
        child: TextField(
          controller: _ctl(qid),
          minLines: 2,
          maxLines: 5,
          maxLength: AskSelection.maxText,
          decoration: InputDecoration(hintText: '${q['placeholder'] ?? Texts.t('ask.free.ph')}', border: const OutlineInputBorder(), isDense: true),
          onChanged: (v) => setState(() => sel.setText(qid, v)),
        ),
      ));
    } else {
      for (final o in (q['options'] as List? ?? const []).whereType<Map>()) {
        final oid = '${o['id']}';
        final label = kind == 'confirm' ? Texts.t(oid == 'yes' ? 'ask.yes' : 'ask.no') : '${o['label']}';
        final rec = kind == 'confirm' ? (q['recommended'] is bool && (q['recommended'] == (oid == 'yes'))) : o['recommended'] == true;
        kids.add(_row(
          multi: multi,
          checked: sel.picked(qid).contains(oid),
          label: label,
          description: kind == 'confirm' ? '' : '${o['description'] ?? ''}',
          recommended: rec,
          onChanged: (on) {
            setState(() => sel.setPick(qid, oid, on));
            if (sel.fast && on) _submit('submitted');
          },
        ));
      }
      if (q['allow_other'] != false && kind != 'confirm') {
        kids.add(_row(
          multi: multi,
          checked: sel.other(qid),
          label: Texts.t('ask.other'),
          onChanged: (on) => setState(() {
            sel.setOther(qid, on);
            if (!on) _ctl(qid).clear();
          }),
        ));
        if (sel.other(qid)) {
          kids.add(Padding(
            padding: const EdgeInsets.only(left: 34, top: 4),
            child: TextField(
              controller: _ctl(qid),
              autofocus: true,
              maxLength: AskSelection.maxText,
              decoration: InputDecoration(hintText: Texts.t('ask.other.ph'), border: const OutlineInputBorder(), isDense: true),
              onChanged: (v) => setState(() => sel.setText(qid, v)),
            ),
          ));
        }
      }
    }
    return Padding(padding: const EdgeInsets.only(bottom: 12), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: kids));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final how = widget.answered;
    if (how != null) {
      final text = how == 'skipped'
          ? Texts.t('ask.skipped')
          : how == 'chat'
              ? Texts.t('ask.in_chat')
              : how.isEmpty
                  ? Texts.t('ask.closed')
                  : Texts.t('ask.answered');
      return Card(
        color: theme.colorScheme.secondaryContainer,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(text, style: theme.textTheme.bodySmall),
            if (how == 'submitted') for (final l in sel.lines()) Text('• $l'),
          ]),
        ),
      );
    }
    final missing = sel.missing();
    final missingText = missing == null ? '' : Texts.t('ask.missing', ['${widget.questions.firstWhere((q) => '${q['id']}' == missing)['text']}']);
    return Card(
      color: theme.colorScheme.secondaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (widget.intro.isNotEmpty) Padding(padding: const EdgeInsets.only(bottom: 8), child: Text(widget.intro)),
          for (var i = 0; i < widget.questions.length; i++) _question(widget.questions[i], i),
          if (missingText.isNotEmpty) Semantics(liveRegion: true, child: Text(missingText, style: theme.textTheme.bodySmall)),
          Wrap(spacing: 8, runSpacing: 4, children: [
            FilledButton(onPressed: missing == null ? () => _submit('submitted') : null, child: Text(Texts.t('ask.send'))),
            OutlinedButton(onPressed: () => _submit('skipped'), child: Text(Texts.t('ask.skip'))),
            TextButton(onPressed: () => _submit('chat'), child: Text(Texts.t('ask.chat'))),
          ]),
        ]),
      ),
    );
  }
}

/// The next-message chips under an answer: a tap puts the text in the message box, never sends it.
class SuggestionChips extends StatelessWidget {
  const SuggestionChips({super.key, required this.items, required this.onPick});

  final List<Map<String, String>> items;
  final void Function(String prompt) onPick;

  @override
  Widget build(BuildContext context) {
    if (items.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      child: Semantics(
        container: true,
        label: Texts.t('ask.suggest.label'),
        child: Wrap(spacing: 8, runSpacing: 4, children: [
          for (final c in items)
            Tooltip(
              message: Texts.t('ask.suggest.tip', [c['prompt']]),
              child: ActionChip(
                materialTapTargetSize: MaterialTapTargetSize.padded,
                label: Text(c['label'] ?? c['prompt'] ?? '', overflow: TextOverflow.ellipsis),
                onPressed: () => onPick(c['prompt'] ?? ''),
              ),
            ),
        ]),
      ),
    );
  }
}
