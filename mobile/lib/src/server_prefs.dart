import 'package:flutter/foundation.dart';

import 'api.dart';
import 'i18n.dart';

/// The roles an agent conversation can have, in the order the PC's selector shows them. The PC lists them only to a client on the PC itself,
/// so the phone carries the list; a role the server does not know is refused with its own message.
const agentRoles = ['build', 'plan', 'ask', 'debug', 'orchestrate', 'general'];

/// The role a conversation in a folder starts with when the person has not chosen: the one that builds code, as on the PC.
const defaultAgentRole = 'build';

/// The switches the PC's chat page keeps on the server, for every conversation: how a request is looked at first (`api/intake`), the
/// rewrite of the request by the technical session (`api/intake`), and "work until done" (`api/persistence`). The server is the one place
/// they live: a change is sent at once and the value the server answers with is the one shown; a refusal puts the old value back.
class ServerPrefs extends ChangeNotifier {
  ServerPrefs(this.api);
  final Api api;

  static const intakeModes = ['off', 'rules', 'full'];

  /// off (default), rules or full.
  String intake = 'off';
  bool optimizePrompt = false;
  bool persistence = true;

  /// Whether the server reports a technical session now (a slot with a context of its own), and its size in tokens.
  bool sideActive = false;
  int sideTokens = 0;

  bool intakeLoaded = false;
  bool persistenceLoaded = false;
  bool reachable = true;
  bool saving = false;
  String note = '';
  bool noteBad = false;
  bool _disposed = false;

  bool get loaded => intakeLoaded || persistenceLoaded;

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  Future<void> load() async {
    reachable = true;
    try {
      final r = await api.get('/api/intake');
      final m = '${r['intake']}';
      if (intakeModes.contains(m)) intake = m;
      optimizePrompt = r['optimize_prompt'] == true;
      intakeLoaded = true;
    } on ApiError catch (e) {
      _fail(e);
    }
    try {
      final r = await api.get('/api/persistence');
      persistence = r['on'] != false;
      persistenceLoaded = true;
    } on ApiError catch (e) {
      _fail(e);
    }
    await refreshSide();
  }

  /// Asks whether the technical session is there, the way the PC's page does (the engine's status, its side slot).
  Future<void> refreshSide() async {
    try {
      final r = await api.get('/v1/status');
      final c = r['concurrency'];
      final slot = c is Map ? c['side_slot'] : null;
      final tokens = slot is Map ? (slot['context'] as num?)?.toInt() ?? 0 : 0;
      sideActive = tokens > 0;
      sideTokens = sideActive ? tokens : 0;
    } on ApiError catch (e) {
      sideActive = false;
      sideTokens = 0;
      _fail(e);
    }
    _changed();
  }

  void _fail(ApiError e) {
    if (e.status == 0) reachable = false;
    note = _why(e);
    noteBad = true;
  }

  /// A phone's key is refused for these settings on some servers: said in the app's words, not the server's.
  String _why(ApiError e) => e.status == 403 ? Texts.t('opts.pconly') : e.message;

  Future<void> setIntake(String mode) async {
    if (!intakeLoaded || saving || !intakeModes.contains(mode) || mode == intake) return;
    final before = intake;
    intake = mode;
    await _save(() async {
      final r = await api.post('/api/intake', {'intake': mode});
      final m = '${r['intake']}';
      intake = intakeModes.contains(m) ? m : mode;
      optimizePrompt = r['optimize_prompt'] == true;
    }, () => intake = before);
  }

  /// Switching it on needs the technical session; switching it off is always possible.
  Future<void> setOptimize(bool on) async {
    if (!intakeLoaded || saving || on == optimizePrompt || (on && !sideActive)) return;
    final before = optimizePrompt;
    optimizePrompt = on;
    await _save(() async {
      final r = await api.post('/api/intake', {'optimize_prompt': on});
      optimizePrompt = r['optimize_prompt'] == true;
    }, () => optimizePrompt = before, refused: Texts.t('opts.optimize.refused'), onRefused: (e) => e.status == 400 ? refreshSide() : null);
  }

  Future<void> setPersistence(bool on) async {
    if (!persistenceLoaded || saving || on == persistence) return;
    final before = persistence;
    persistence = on;
    await _save(() async {
      final r = await api.post('/api/persistence', {'on': on});
      persistence = r['on'] == true;
    }, () => persistence = before);
  }

  Future<void> _save(Future<void> Function() send, void Function() undo, {String? refused, Future<void>? Function(ApiError e)? onRefused}) async {
    saving = true;
    note = '';
    noteBad = false;
    _changed();
    try {
      await send();
      note = Texts.t('opts.saved');
    } on ApiError catch (e) {
      undo();
      note = e.status == 400 && refused != null ? refused : _why(e);
      noteBad = true;
      await onRefused?.call(e);
    }
    saving = false;
    _changed();
  }
}
