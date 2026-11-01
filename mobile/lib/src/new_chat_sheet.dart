import 'package:flutter/material.dart';

import 'chat_model.dart';
import 'chat_options.dart';
import 'i18n.dart';
import 'server_prefs.dart';
import 'settings.dart';

/// The fields the PC's page asks for when it makes a conversation: how much it may do alone, how hard it thinks, the rules that let
/// commands through, and (in a folder) whether it works on a copy of the files. Returns null when the sheet is closed without starting.
Future<NewChat?> showNewChatSheet(BuildContext context, AppSettings settings, {required bool folder}) {
  return showModalBottomSheet<NewChat>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (c) => Padding(padding: EdgeInsets.only(bottom: MediaQuery.of(c).viewInsets.bottom), child: _NewChatForm(settings: settings, folder: folder)),
  );
}

class _NewChatForm extends StatefulWidget {
  const _NewChatForm({required this.settings, required this.folder});
  final AppSettings settings;
  final bool folder;
  @override
  State<_NewChatForm> createState() => _NewChatFormState();
}

class _NewChatFormState extends State<_NewChatForm> {
  late String mode = widget.settings.mode;
  String effort = '';
  String worktree = 'auto';
  late String role = widget.settings.agentRole.isEmpty ? defaultAgentRole : widget.settings.agentRole;
  final rules = TextEditingController();

  @override
  void dispose() {
    rules.dispose();
    super.dispose();
  }

  List<String> get allow => rules.text.split('\n').map((l) => l.trim()).where((l) => l.isNotEmpty).toList();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
        Text(Texts.t('new.title'), style: theme.textTheme.titleMedium),
        const SizedBox(height: 12),
        DropdownButtonFormField<String>(
          initialValue: mode,
          isExpanded: true,
          decoration: InputDecoration(labelText: Texts.t('set.mode'), helperText: Texts.t('mode.$mode.d'), helperMaxLines: 3, border: const OutlineInputBorder()),
          items: [for (final m in const ['plan', 'read_only', 'default', 'auto']) DropdownMenuItem(value: m, child: Text(Texts.t('mode.$m')))],
          onChanged: (v) => setState(() => mode = v ?? mode),
        ),
        const SizedBox(height: 14),
        DropdownButtonFormField<String>(
          initialValue: effort,
          isExpanded: true,
          decoration: InputDecoration(labelText: Texts.t('new.effort'), helperText: Texts.t('new.effort.help'), helperMaxLines: 3, border: const OutlineInputBorder()),
          items: [for (final e in const ['', 'medium', 'low', 'none']) DropdownMenuItem(value: e, child: Text(Texts.t('new.effort.${e.isEmpty ? 'high' : e}')))],
          onChanged: (v) => setState(() => effort = v ?? effort),
        ),
        const SizedBox(height: 14),
        TextField(
          controller: rules,
          minLines: 2,
          maxLines: 6,
          autocorrect: false,
          enableSuggestions: false,
          decoration: InputDecoration(labelText: Texts.t('new.rules'), hintText: 'shell_command:git *', helperText: Texts.t('new.rules.help'), helperMaxLines: 4, border: const OutlineInputBorder()),
        ),
        if (widget.folder) ...[
          const SizedBox(height: 14),
          RolePicker(role: role, onChanged: (v) => setState(() => role = v)),
          const SizedBox(height: 14),
          DropdownButtonFormField<String>(
            initialValue: worktree,
            isExpanded: true,
            decoration: InputDecoration(labelText: Texts.t('new.worktree'), helperText: Texts.t('new.worktree.$worktree.d'), helperMaxLines: 4, border: const OutlineInputBorder()),
            items: [for (final w in const ['off', 'auto', 'always']) DropdownMenuItem(value: w, child: Text(Texts.t('new.worktree.$w')))],
            onChanged: (v) => setState(() => worktree = v ?? worktree),
          ),
        ],
        const SizedBox(height: 10),
        Text(Texts.t('new.model.note'), style: theme.textTheme.bodySmall),
        const SizedBox(height: 14),
        SizedBox(
          width: double.infinity,
          child: FilledButton.icon(
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            onPressed: () {
              if (widget.folder) widget.settings.setAgentRole(role);
              Navigator.pop(context, NewChat(mode: mode, effort: effort, allow: allow, worktree: widget.folder ? worktree : 'auto', role: widget.folder ? role : ''));
            },
            icon: const Icon(Icons.play_arrow),
            label: Text(Texts.t('new.start')),
          ),
        ),
      ]),
    );
  }
}
