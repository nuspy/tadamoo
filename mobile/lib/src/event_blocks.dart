import 'dart:convert';

import 'package:flutter/material.dart';

import 'chat_model.dart';
import 'i18n.dart';

/// Each background command has a colour of its own, taken in turn; the state is also said in words and by an icon.
const jobColours = [Color(0xFF4FC3F7), Color(0xFFAED581), Color(0xFFFFB74D), Color(0xFFBA68C8), Color(0xFF4DB6AC), Color(0xFFF06292)];

/// A command the agent left running in the background: its command, its state and exit code, and the output as it is written.
class JobBlock extends StatelessWidget {
  const JobBlock({super.key, required this.block});
  final Block block;

  String get _state {
    final word = Texts.t('job.${block.status}');
    final code = block.code;
    return code != null && block.status != 'running' ? '$word ($code)' : word;
  }

  bool get _failed => block.status == 'killed' || (block.status == 'finished' && (block.code ?? 0) != 0);

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colour = jobColours[block.n % jobColours.length];
    final mono = TextStyle(fontFamily: 'monospace', fontSize: 12, color: theme.colorScheme.onSurface);
    final icon = block.status == 'running' ? Icons.hourglass_empty : _failed ? Icons.error_outline : Icons.check_circle_outline;
    return Semantics(
      container: true,
      label: Texts.t('job.label', [block.id, block.s, _state]),
      child: Card(
        margin: const EdgeInsets.symmetric(vertical: 3),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12), side: BorderSide(color: colour, width: 1.5)),
        child: ExpansionTile(
          key: PageStorageKey('job-${block.id}'),
          dense: true,
          initiallyExpanded: true,
          leading: Icon(icon, size: 20, color: colour),
          title: Text(block.s.isEmpty ? block.id : block.s, maxLines: 2, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodySmall?.copyWith(fontFamily: 'monospace')),
          subtitle: Text('${block.id} · $_state', style: theme.textTheme.bodySmall),
          children: [
            if (block.segs.isNotEmpty)
              ConstrainedBox(
                constraints: const BoxConstraints(maxHeight: 240),
                child: SingleChildScrollView(
                  reverse: true,
                  padding: const EdgeInsets.fromLTRB(14, 0, 14, 10),
                  child: Align(
                    alignment: Alignment.centerLeft,
                    child: SelectableText.rich(TextSpan(children: [
                      for (final g in block.segs) TextSpan(text: g.text, style: g.err ? TextStyle(color: theme.colorScheme.error, fontStyle: FontStyle.italic) : null),
                    ], style: mono)),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// A question the harness puts because text read from outside gave orders to the AI, or because a hook of the person's asks: in the app's
/// words, with the safe answer as the main button. The server's own sentence and button labels are English, so they are not used.
class OutsideAskCard extends StatelessWidget {
  const OutsideAskCard({super.key, required this.block, required this.onAnswer});
  final Block block;
  final void Function(String value) onAnswer;

  bool get _hook => block.instructions.isEmpty;

  String get _call {
    var args = '';
    try {
      args = jsonEncode(block.args);
    } catch (_) {
      args = block.args.toString();
    }
    if (args.length > 300) args = args.substring(0, 300);
    return '${block.name} $args'.trim();
  }

  String _wordsOf(String value) => value == 'confirm' ? Texts.t(_hook ? 'ask.hook.yes' : 'ask.instructions.yes') : Texts.t('ask.outside.no');

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final answered = block.answered != null;
    final hasOptions = block.options.isNotEmpty;
    final values = hasOptions ? block.options.map((o) => '${o['value']}').toList() : const ['confirm', 'cancel'];
    final sentence = _hook ? Texts.t('ask.hook', [block.hook, _call]) : Texts.t('ask.instructions', [block.instructions, _call]);
    final tall = ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(64, 48)));
    final on = theme.colorScheme.onErrorContainer;
    final outlined = OutlinedButton.styleFrom(minimumSize: const Size(64, 48), foregroundColor: on, side: BorderSide(color: on));
    return Semantics(
      container: true,
      label: Texts.t(_hook ? 'ask.hook.title' : 'ask.instructions.title'),
      child: Card(
        color: theme.colorScheme.errorContainer,
        margin: const EdgeInsets.symmetric(vertical: 4),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Icon(_hook ? Icons.webhook_outlined : Icons.gpp_maybe_outlined, color: theme.colorScheme.onErrorContainer),
              const SizedBox(width: 8),
              Expanded(child: Text(Texts.t(_hook ? 'ask.hook.title' : 'ask.instructions.title'), style: theme.textTheme.titleSmall?.copyWith(color: theme.colorScheme.onErrorContainer))),
            ]),
            const SizedBox(height: 8),
            SelectableText(sentence, style: TextStyle(color: theme.colorScheme.onErrorContainer)),
            const SizedBox(height: 10),
            Wrap(spacing: 8, runSpacing: 8, children: [
              for (final v in values)
                v == 'confirm'
                    ? OutlinedButton(key: const ValueKey('outside-yes'), style: outlined, onPressed: answered ? null : () => onAnswer(v), child: Text(_wordsOf(v)))
                    : FilledButton(key: const ValueKey('outside-no'), style: tall, onPressed: answered ? null : () => onAnswer(v), child: Text(_wordsOf(v))),
            ]),
            if (answered && (block.answered ?? '').isNotEmpty)
              Padding(padding: const EdgeInsets.only(top: 6), child: Text(Texts.t('chat.answered', [_wordsOf(block.answered!)]), style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onErrorContainer))),
          ]),
        ),
      ),
    );
  }
}
