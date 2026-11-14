import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_svg/flutter_svg.dart';
import 'package:markdown/markdown.dart' as md;
import 'package:url_launcher/url_launcher.dart';

import 'api.dart';
import 'i18n.dart';
import 'net_address.dart';

typedef FileTap = void Function(String kind, String ref, String name);

final _mermaid = RegExp(r'```mermaid[ \t]*\n([\s\S]*?)```', multiLine: true);
final _fileLink = RegExp(r'(?<!!)\[([^\]\n]+)\]\((tadamoo-file:[a-f0-9]{12}|tadamoo-path:[^)\s]{1,400})\)');
final _pictureLink = RegExp(r'(?<!!)\[([^\]\n]+)\]\((tadamoo-path:[^)\s]{1,400}\.(?:png|jpe?g|gif|webp|svg))\)', caseSensitive: false);

/// An answer as the person reads it: Markdown, a button for each link to a file, the pictures the agent kept or made, and a diagram's source
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
    final text = fixAddresses(this.text, api.settings.base);          // an address of the PC's own machine in the answer becomes the one this app reaches it at
    for (final m in _mermaid.allMatches(text)) {
      if (m.start > at) parts.add(_markdown(context, text.substring(at, m.start)));
      parts.add(_Diagram(source: m.group(1) ?? ''));
      at = m.end;
    }
    if (at < text.length) parts.add(_markdown(context, text.substring(at)));
    // the text is selected by the area, not by each paragraph: a link inside selectable text does not take the tap
    return SelectionArea(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: parts));
  }

  Widget _markdown(BuildContext context, String piece) {
    // a link to a picture shows it, then the button that saves it; a link to any other file gets an arrow so it reads as something that saves
    final shown = piece.replaceAllMapped(_pictureLink, (m) => '![${m.group(1)}](${m.group(2)})\n\n[${m.group(1)}](${m.group(2)})');
    final marked = shown.replaceAllMapped(_fileLink, (m) => '[↓ ${m.group(1)}](${m.group(2)})');
    final theme = Theme.of(context);
    return MarkdownBody(
      data: marked,
      extensionSet: md.ExtensionSet.gitHubFlavored,
      styleSheet: MarkdownStyleSheet.fromTheme(theme).copyWith(
        code: TextStyle(fontFamily: 'monospace', fontSize: 13, backgroundColor: theme.colorScheme.surfaceContainerHighest),
        codeblockDecoration: BoxDecoration(color: theme.colorScheme.surfaceContainerHighest, borderRadius: BorderRadius.circular(8)),
        a: TextStyle(color: theme.colorScheme.primary, decoration: TextDecoration.underline),
      ),
      onTapLink: (label, href, title) async {
        if (href == null) return;
        if (href.startsWith('tadamoo-file:')) {
          onFile('file', href.substring('tadamoo-file:'.length), label.replaceFirst('↓ ', ''));
        } else if (href.startsWith('tadamoo-path:')) {
          onFile('path', Uri.decodeFull(href.substring('tadamoo-path:'.length)), label.replaceFirst('↓ ', ''));
        } else {
          final uri = Uri.tryParse(href);
          if (uri != null && (uri.scheme == 'http' || uri.scheme == 'https')) await launchUrl(uri, mode: LaunchMode.externalApplication);
        }
      },
      // a picture the agent kept or made in the conversation, or one the answer carries as data; never an address written in the text
      imageBuilder: (uri, title, alt) => _picture(uri, alt),
    );
  }

  Widget _picture(Uri uri, String? alt) {
    final Widget? image = _image(uri);
    if (image == null) return Text(alt ?? '', style: const TextStyle(fontStyle: FontStyle.italic));
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        ClipRRect(borderRadius: BorderRadius.circular(8), child: image),
        if ((alt ?? '').isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Text(alt!, style: const TextStyle(fontSize: 12.5, color: Colors.white60))),
      ]),
    );
  }

  Widget? _image(Uri uri) {
    Widget fail(BuildContext context, Object error, StackTrace? stack) => const SizedBox.shrink();
    if (uri.scheme == 'data' && uri.data != null && uri.data!.mimeType.startsWith('image/') && uri.data!.mimeType != 'image/svg+xml') {
      return Image.memory(uri.data!.contentAsBytes(), fit: BoxFit.contain, errorBuilder: fail);
    }
    final String url;
    if (uri.scheme == 'tadamoo-file') {
      final id = uri.path.isNotEmpty ? uri.path : uri.toString().substring('tadamoo-file:'.length);
      url = api.uri('/api/outputs/${Uri.encodeComponent(sid)}/$id/file').toString();
    } else if (uri.scheme == 'tadamoo-path') {
      final path = Uri.decodeFull(uri.toString().substring('tadamoo-path:'.length));
      url = api.uri('/api/agent/sessions/${Uri.encodeComponent(sid)}/file', {'path': path, 'inline': '1'}).toString();
      if (path.toLowerCase().endsWith('.svg')) return SvgPicture.network(url, headers: api.headers, fit: BoxFit.contain);
    } else {
      return null;
    }
    return Image.network(url, headers: api.headers, fit: BoxFit.contain, errorBuilder: fail);
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
        children: [Padding(padding: const EdgeInsets.all(12), child: Text(source, style: const TextStyle(fontFamily: 'monospace', fontSize: 12.5)))],
      ),
    );
  }
}
