import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:url_launcher/url_launcher.dart';

import 'api.dart';
import 'i18n.dart';

typedef FileTap = void Function(String kind, String ref, String name);

final _mermaid = RegExp(r'```mermaid[ \t]*\n([\s\S]*?)```', multiLine: true);
final _fileLink = RegExp(r'\[([^\]\n]+)\]\((basalt-file:[a-f0-9]{12}|basalt-path:[^)\s]{1,400})\)');

/// An answer as the person reads it: Markdown, a button for each link to a file, the pictures the agent kept, and a diagram's source
/// in a box that opens (the PC draws diagrams; here the text of one is what is shown).
class AnswerView extends StatelessWidget {
  const AnswerView({super.key, required this.text, required this.api, required this.sid, required this.onFile});
  final String text;
  final Api api;
  final String sid;
  final FileTap onFile;

  @override
  Widget build(BuildContext context) {
    final parts = <Widget>[];
    var at = 0;
    for (final m in _mermaid.allMatches(text)) {
      if (m.start > at) parts.add(_markdown(context, text.substring(at, m.start)));
      parts.add(_Diagram(source: m.group(1) ?? ''));
      at = m.end;
    }
    if (at < text.length) parts.add(_markdown(context, text.substring(at)));
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: parts);
  }

  Widget _markdown(BuildContext context, String piece) {
    // a link to a file gets an arrow so it reads as something that saves
    final marked = piece.replaceAllMapped(_fileLink, (m) => '[↓ ${m.group(1)}](${m.group(2)})');
    final theme = Theme.of(context);
    return MarkdownBody(
      data: marked,
      selectable: true,
      styleSheet: MarkdownStyleSheet.fromTheme(theme).copyWith(
        code: TextStyle(fontFamily: 'monospace', fontSize: 13, backgroundColor: theme.colorScheme.surfaceContainerHighest),
        codeblockDecoration: BoxDecoration(color: theme.colorScheme.surfaceContainerHighest, borderRadius: BorderRadius.circular(8)),
        a: TextStyle(color: theme.colorScheme.primary, decoration: TextDecoration.underline),
      ),
      onTapLink: (label, href, title) async {
        if (href == null) return;
        if (href.startsWith('basalt-file:')) {
          onFile('file', href.substring(12), label.replaceFirst('↓ ', ''));
        } else if (href.startsWith('basalt-path:')) {
          onFile('path', Uri.decodeFull(href.substring(12)), label.replaceFirst('↓ ', ''));
        } else {
          final uri = Uri.tryParse(href);
          if (uri != null && (uri.scheme == 'http' || uri.scheme == 'https')) await launchUrl(uri, mode: LaunchMode.externalApplication);
        }
      },
      // only a picture the agent kept in the conversation is shown, never an address written in the text
      imageBuilder: (uri, title, alt) => _picture(uri, alt),
    );
  }

  Widget _picture(Uri uri, String? alt) {
    if (uri.scheme != 'basalt-file') return Text(alt ?? '', style: const TextStyle(fontStyle: FontStyle.italic));
    final id = uri.path.isNotEmpty ? uri.path : uri.toString().substring('basalt-file:'.length);
    final url = api.uri('/api/outputs/${Uri.encodeComponent(sid)}/$id/file').toString();
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        ClipRRect(
          borderRadius: BorderRadius.circular(8),
          child: Image.network(url, headers: api.headers, fit: BoxFit.contain, errorBuilder: (context, error, stack) => Text(alt ?? '')),
        ),
        if ((alt ?? '').isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Text(alt!, style: const TextStyle(fontSize: 12.5, color: Colors.white60))),
      ]),
    );
  }
}

class _Diagram extends StatelessWidget {
  const _Diagram({required this.source});
  final String source;
  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 6),
      child: ExpansionTile(
        leading: const Icon(Icons.account_tree_outlined),
        title: Text(Texts.t('chat.diagram')),
        children: [Padding(padding: const EdgeInsets.all(12), child: SelectableText(source, style: const TextStyle(fontFamily: 'monospace', fontSize: 12.5)))],
      ),
    );
  }
}
