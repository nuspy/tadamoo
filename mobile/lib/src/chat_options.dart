import 'package:flutter/material.dart';

import 'i18n.dart';
import 'server_prefs.dart';

/// The words for a role in the app's language.
String roleName(String id) => Texts.t('role.$id');

/// The list of roles with what each one is for, as a menu: used in the options sheet and in the sheet that starts a conversation in a folder.
class RolePicker extends StatelessWidget {
  const RolePicker({super.key, required this.role, required this.onChanged, this.enabled = true});
  final String role;
  final ValueChanged<String> onChanged;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    final value = agentRoles.contains(role) ? role : defaultAgentRole;
    return DropdownButtonFormField<String>(
      key: ValueKey('role-$value'),
      initialValue: value,
      isExpanded: true,
      decoration: InputDecoration(labelText: Texts.t('role.title'), helperText: Texts.t('role.$value.d'), helperMaxLines: 4, border: const OutlineInputBorder()),
      items: [for (final r in agentRoles) DropdownMenuItem(value: r, child: Text(roleName(r)))],
      onChanged: enabled
          ? (v) {
              if (v != null && v != value) onChanged(v);
            }
          : null,
    );
  }
}

/// Opens the options of a conversation: how a request is looked at, the rewrite of the request, "work until done", the technical session,
/// and (in a folder) the role. `onRole` returns the server's complaint, or null when the role was changed.
Future<void> showChatOptions(BuildContext context, {required ServerPrefs prefs, String? role, Future<String?> Function(String role)? onRole}) {
  prefs.load();
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (c) => Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.of(c).viewInsets.bottom),
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
          Text(Texts.t('opts.title'), style: Theme.of(c).textTheme.titleMedium),
          const SizedBox(height: 12),
          if (onRole != null) ...[
            _RoleRow(role: role ?? defaultAgentRole, onRole: onRole),
            const SizedBox(height: 16),
          ],
          ServerOptionsBody(prefs: prefs),
        ]),
      ),
    ),
  );
}

class _RoleRow extends StatefulWidget {
  const _RoleRow({required this.role, required this.onRole});
  final String role;
  final Future<String?> Function(String role) onRole;
  @override
  State<_RoleRow> createState() => _RoleRowState();
}

class _RoleRowState extends State<_RoleRow> {
  late String role = widget.role;
  String problem = '';
  bool busy = false;

  Future<void> _pick(String id) async {
    setState(() {
      busy = true;
      problem = '';
    });
    final complaint = await widget.onRole(id);
    if (!mounted) return;
    setState(() {
      busy = false;
      if (complaint == null) {
        role = id;
      } else {
        problem = complaint;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      RolePicker(role: role, enabled: !busy, onChanged: _pick),
      if (problem.isNotEmpty) Semantics(liveRegion: true, child: Padding(padding: const EdgeInsets.only(top: 6), child: Text(problem, style: TextStyle(color: Theme.of(context).colorScheme.error)))),
    ]);
  }
}

/// The switches the server keeps for every conversation. It draws what the [ServerPrefs] hold and sends the changes through them.
class ServerOptionsBody extends StatelessWidget {
  const ServerOptionsBody({super.key, required this.prefs});
  final ServerPrefs prefs;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(listenable: prefs, builder: (c, _) => _body(c));
  }

  Widget _body(BuildContext context) {
    final theme = Theme.of(context);
    final canIntake = prefs.intakeLoaded && !prefs.saving;
    final side = prefs.sideActive;
    // Saved as on while there is no technical session: nothing is rewritten now, and the box can only be cleared.
    final stale = !side && prefs.optimizePrompt;
    final optimizeEnabled = prefs.intakeLoaded && !prefs.saving && (side || prefs.optimizePrompt);
    final optimizeWhy = !prefs.intakeLoaded || side
        ? ''
        : stale
            ? Texts.t('po.why.kept')
            : prefs.reachable
                ? Texts.t('po.why.off')
                : Texts.t('po.why.offline');
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Row(children: [
        Expanded(child: Text(Texts.t('ps.title'), style: theme.textTheme.titleSmall)),
        IconButton(tooltip: Texts.t('opts.reload'), icon: const Icon(Icons.refresh), constraints: const BoxConstraints(minWidth: 48, minHeight: 48), onPressed: prefs.saving ? null : prefs.load),
      ]),
      Semantics(
        label: Texts.t('ps.title'),
        container: true,
        child: RadioGroup<String>(
          groupValue: prefs.intake,
          onChanged: (v) => canIntake && v != null ? prefs.setIntake(v) : null,
          child: Column(children: [
            for (final m in ServerPrefs.intakeModes)
              RadioListTile<String>(
                key: ValueKey('ps-$m'),
                value: m,
                enabled: canIntake,
                contentPadding: EdgeInsets.zero,
                title: Text(Texts.t('ps.$m')),
                subtitle: Text(Texts.t('ps.$m.d')),
              ),
          ]),
        ),
      ),
      const Divider(height: 24),
      CheckboxListTile(
        key: const ValueKey('opt-prompt'),
        contentPadding: EdgeInsets.zero,
        controlAffinity: ListTileControlAffinity.leading,
        tristate: stale,
        value: stale ? null : prefs.optimizePrompt,
        onChanged: optimizeEnabled ? (v) => prefs.setOptimize(side ? v == true : false) : null,
        title: Text(Texts.t('po.title')),
        subtitle: Text([Texts.t('po.tip'), if (optimizeWhy.isNotEmpty) optimizeWhy].join('\n')),
      ),
      SwitchListTile(
        key: const ValueKey('work-until-done'),
        contentPadding: EdgeInsets.zero,
        value: prefs.persistence,
        onChanged: prefs.persistenceLoaded && !prefs.saving ? prefs.setPersistence : null,
        title: Text(Texts.t('wud.title')),
        subtitle: Text(Texts.t('wud.tip')),
      ),
      // TODO(technical-session): the server has no route a phone can use to switch the technical session on or off (it is a Loader
      // setting of the PC, applied by loading the model again); until it has one this shows the state and cannot be changed.
      SwitchListTile(
        key: const ValueKey('technical-session'),
        contentPadding: EdgeInsets.zero,
        value: side,
        onChanged: null,
        title: Text(Texts.t('side.title')),
        subtitle: Text([side ? Texts.t('side.on', [prefs.sideTokens]) : Texts.t('side.off'), Texts.t('side.help')].join('\n')),
      ),
      if (prefs.note.isNotEmpty)
        Semantics(
          liveRegion: true,
          child: Padding(padding: const EdgeInsets.only(top: 6), child: Text(prefs.note, style: TextStyle(color: prefs.noteBad ? theme.colorScheme.error : null))),
        ),
    ]);
  }
}
