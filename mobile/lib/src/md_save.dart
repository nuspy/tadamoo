import 'package:flutter/material.dart';

import 'api.dart';
import 'i18n.dart';
import 'settings.dart';

/// What the person chose to save of a Markdown file: `md` (only the Markdown file), `converted` (only the file already converted) or `both`
/// (the Markdown and the converted one, in one zip). The format is one of the PC's (`/api/export/formats`); it is '' for `md`.
class MdSaveChoice {
  const MdSaveChoice(this.mode, this.format);
  final String mode;
  final String format;
  static const md = MdSaveChoice('md', '');
  static const modes = ['md', 'converted', 'both'];
}

/// A format the PC can make a Markdown file into.
class MdFormat {
  const MdFormat(this.id, this.label);
  final String id;
  final String label;
}

/// Whether a file name is a Markdown file.
bool isMarkdownName(String name) => RegExp(r'\.(md|markdown)$', caseSensitive: false).hasMatch(name);

/// The route and query that convert a Markdown file of the conversation (`kind` file: one the agent made, `path`: one in its folder).
(String, Map<String, String>) mdConvertRequest(String kind, String sid, String ref, String mode, String format, String lang) {
  final q = {'format': format, 'mode': mode, 'lang': lang};
  return kind == 'file'
      ? ('/api/outputs/${Uri.encodeComponent(sid)}/$ref/convert', q)
      : ('/api/agent/sessions/${Uri.encodeComponent(sid)}/file-convert', {'path': ref, ...q});
}

extension MdFormats on Api {
  /// The formats this PC can make a Markdown file into, in the app's language; empty when the PC cannot convert (an older one).
  Future<List<MdFormat>> mdFormats() async {
    try {
      final r = await get('/api/export/formats', query: {'lang': Texts.language});
      return [for (final f in (r['formats'] as List? ?? const [])) if (f is Map) MdFormat('${f['id']}', '${f['label']}')];
    } on ApiError {
      return const [];
    }
  }
}

/// Asks what to save of a Markdown file. Returns null when the sheet is closed without a choice. With a PC that cannot convert there is nothing to ask:
/// the answer is the Markdown file alone, as it always was.
Future<MdSaveChoice?> askMdSave(BuildContext context, Api api, AppSettings settings, String name) async {
  final formats = await api.mdFormats();
  if (formats.isEmpty || !context.mounted) return MdSaveChoice.md;
  return showModalBottomSheet<MdSaveChoice>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (c) => Padding(padding: EdgeInsets.only(bottom: MediaQuery.of(c).viewInsets.bottom), child: MdSaveForm(name: name, formats: formats, settings: settings)),
  );
}

class MdSaveForm extends StatefulWidget {
  const MdSaveForm({super.key, required this.name, required this.formats, required this.settings});
  final String name;
  final List<MdFormat> formats;
  final AppSettings settings;
  @override
  State<MdSaveForm> createState() => _MdSaveFormState();
}

class _MdSaveFormState extends State<MdSaveForm> {
  late String mode = MdSaveChoice.modes.contains(widget.settings.mdMode) ? widget.settings.mdMode : 'md';
  late String format = widget.formats.any((f) => f.id == widget.settings.mdFormat) ? widget.settings.mdFormat : widget.formats.first.id;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
        Text(Texts.t('mx.title', [widget.name]), style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        RadioGroup<String>(
          groupValue: mode,
          onChanged: (v) => setState(() => mode = v ?? mode),
          child: Column(children: [
            for (final m in MdSaveChoice.modes)
              RadioListTile<String>(key: ValueKey('mx-$m'), contentPadding: EdgeInsets.zero, value: m, title: Text(Texts.t('mx.$m'))),
          ]),
        ),
        const SizedBox(height: 8),
        DropdownButtonFormField<String>(
          key: ValueKey('mx-format-$mode'),
          initialValue: format,
          isExpanded: true,
          decoration: InputDecoration(labelText: Texts.t('mx.format'), border: const OutlineInputBorder()),
          items: [for (final f in widget.formats) DropdownMenuItem(value: f.id, child: Text(f.label))],
          onChanged: mode == 'md' ? null : (v) => setState(() => format = v ?? format),
        ),
        const SizedBox(height: 16),
        Row(children: [
          Expanded(child: TextButton(style: TextButton.styleFrom(minimumSize: const Size.fromHeight(48)), onPressed: () => Navigator.pop(context), child: Text(Texts.t('mx.cancel')))),
          const SizedBox(width: 8),
          Expanded(
            child: FilledButton(
              style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
              onPressed: () {
                widget.settings.setMdSave(mode, format);
                Navigator.pop(context, mode == 'md' ? MdSaveChoice.md : MdSaveChoice(mode, format));
              },
              child: Text(Texts.t('mx.go')),
            ),
          ),
        ]),
      ]),
    );
  }
}
