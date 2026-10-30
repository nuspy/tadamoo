import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api.dart';
import 'i18n.dart';
import 'settings.dart';

const shownOutput = 1500;

/// One conversation as the server lists it.
class SessionInfo {
  SessionInfo(this.raw);
  final Map<String, dynamic> raw;
  String get id => '${raw['id']}';
  String get title => '${raw['title'] ?? ''}';
  String get workspace => '${raw['workspace'] ?? ''}';
  String get state => '${raw['state'] ?? ''}';
  String get mode => '${raw['mode'] ?? 'default'}';
  bool get favorite => raw['favorite'] == true;
  double get updated => (raw['updated'] as num?)?.toDouble() ?? 0;
  bool get busy => state == 'busy' || state == 'starting' || state == 'asking';
}

/// A part of an answer, in the order things happened: text, reasoning, a tool step, a question, a note.
class Block {
  Block(this.t, {this.s = '', this.id = '', this.name = ''});
  String t; // text, reasoning, tool, ask, blocked, note
  String s;
  String id;
  String name;
  Map<String, dynamic> args = {};
  String status = 'running';
  String output = '';
  String mode = '';
  String kind = '';
  String question = '';
  String intro = '';
  List<Map<String, dynamic>> options = [];
  List<Map<String, dynamic>> questions = []; // a card of questions: single, multi, free and confirm
  Map<String, dynamic> facts = {};
  String? answered;
  String reason = '';
  String detail = '';
  bool over = false;
}

class ChatItem {
  ChatItem(this.kind, {this.text = '', this.ts = 0, this.turn = ''});
  final String kind; // user, assistant, note
  String text;
  double ts;
  String turn;
  final List<Block> blocks = [];
  List<String> files = [];
  bool done = false;
  String meta = '';
  String how = '';
  bool over = false;

  /// The answer's own text, for copying and for reading aloud.
  String get answerText => blocks.where((b) => b.t == 'text').map((b) => b.s).join('\n\n').trim();
}

/// The conversation that is open: its messages, kept up to date by asking the server for what happened since the last time.
class ChatController extends ChangeNotifier {
  ChatController(this.api, this.settings);
  final Api api;
  final AppSettings settings;

  String sid = '';
  SessionInfo? info;
  final List<ChatItem> items = [];
  List<Map<String, String>> suggestions = []; // the next-message chips under the last answer; a tap fills the message box
  String state = 'dormant';
  int _after = 0;
  Timer? _timer;
  bool _disposed = false;
  bool polling = false;
  String? error;
  final Map<String, ChatItem> _byTurn = {};
  ChatItem? _current;
  double? _stamp;
  void Function(ChatItem answer)? onAnswerDone;

  bool get busy => state == 'busy' || state == 'starting' || state == 'asking';
  bool get ready => sid.isNotEmpty;

  // ---- opening ----
  Future<void> open(String id) async {
    _timer?.cancel();
    sid = id;
    items.clear();
    _byTurn.clear();
    _current = null;
    _after = 0;
    error = null;
    try {
      final h = await api.get('/api/agent/sessions/$id/history');
      info = SessionInfo(Map<String, dynamic>.from(h['session'] as Map));
      state = info!.state;
      if (state == 'busy' || state == 'starting' || state == 'asking') {
        items.clear();
        _byTurn.clear();
        _current = null;
        final e = await api.get('/api/agent/sessions/$id/events', query: {'after': '0'});
        _applyAll(e);
      } else {
        _fromHistory((h['messages'] as List?) ?? const []);
        final e = await api.get('/api/agent/sessions/$id/events', query: {'after': '0'});
        _after = (e['next'] as num?)?.toInt() ?? 0;
        _showWaiting(info!.raw);
      }
    } on ApiError catch (e) {
      error = e.message;
    }
    notifyListeners();
    _schedule();
  }

  /// A new conversation, made in a folder (empty: the Chat's own), with the options the person chose.
  Future<void> create({String workspace = '', required NewChat options}) async {
    final r = await api.post('/api/agent/sessions', {
      if (workspace.isNotEmpty) 'workspace': workspace,
      ...options.body(),
    });
    final rec = Map<String, dynamic>.from(r['session'] as Map);
    sid = '${rec['id']}';
    info = SessionInfo(rec);
    state = info!.state;
    items.clear();
    _byTurn.clear();
    _current = null;
    _after = 0;
    notifyListeners();
    _schedule();
  }

  @override
  void dispose() {
    _disposed = true;
    _timer?.cancel();
    super.dispose();
  }

  void pause() => _timer?.cancel();
  void resume() => _schedule();

  // ---- following ----
  void _schedule([bool fast = false]) {
    _timer?.cancel();
    if (_disposed || sid.isEmpty) return;
    _timer = Timer(Duration(milliseconds: fast || busy ? 350 : 3000), _poll);
  }

  Future<void> _poll() async {
    if (_disposed || sid.isEmpty || polling) return;
    polling = true;
    try {
      final e = await api.get('/api/agent/sessions/$sid/events', query: {'after': '$_after'});
      error = null;
      final before = state;
      final had = (e['events'] as List?)?.length ?? 0;
      _applyAll(e);
      if (had > 0 || before != state) notifyListeners();
    } on ApiError catch (e) {
      error = e.message;
      if (e.status == 404) {
        sid = '';
      }
      notifyListeners();
    } finally {
      polling = false;
    }
    _schedule();
  }

  /// Applies events as the server sent them (for the tests, which have no server).
  @visibleForTesting
  void applyEvents(List<Map<String, dynamic>> events) => _applyAll({'events': events});

  void _applyAll(Map<String, dynamic> e) {
    for (final ev in (e['events'] as List? ?? const [])) {
      if (ev is Map) _apply(Map<String, dynamic>.from(ev));
    }
    _after = (e['next'] as num?)?.toInt() ?? _after;
    state = '${e['state'] ?? state}';
  }

  // ---- the view model ----
  double _now() => _stamp ?? DateTime.now().millisecondsSinceEpoch / 1000;

  ChatItem _assistant(String turn) {
    if (turn.isNotEmpty && _byTurn.containsKey(turn)) return _current = _byTurn[turn]!;
    if (turn.isEmpty && _current != null && !_current!.done) return _current!;
    final a = ChatItem('assistant', ts: _now(), turn: turn);
    _current = a;
    if (turn.isNotEmpty) _byTurn[turn] = a;
    items.add(a);
    return a;
  }

  void _note(String text, {String turn = '', String how = ''}) => items.add(ChatItem('note', text: text, ts: _now(), turn: turn)..how = how);

  void _textBlock(String kind, String text, String turn) {
    final a = _assistant(turn);
    final last = a.blocks.isEmpty ? null : a.blocks.last;
    if (last != null && last.t == kind) {
      last.s += text;
      return;
    }
    if (last != null && last.t == 'reasoning') last.over = true;
    a.blocks.add(Block(kind, s: text));
  }

  Block _addBlock(Block b, String turn) {
    final a = _assistant(turn);
    if (a.blocks.isNotEmpty && a.blocks.last.t == 'reasoning') a.blocks.last.over = true;
    a.blocks.add(b);
    return b;
  }

  Block? _toolBlock(String id) {
    for (final it in items) {
      for (final b in it.blocks) {
        if (b.t == 'tool' && b.id == id) return b;
      }
    }
    return null;
  }

  void _apply(Map<String, dynamic> e) {
    _stamp = (e['t'] as num?)?.toDouble();
    try {
      _applyEvent(e);
    } finally {
      _stamp = null;
    }
  }

  /// What waited for the person when the conversation was opened: a card of questions and the chips of the last answer.
  void _showWaiting(Map<String, dynamic> raw) {
    final q = raw['question'];
    if (q is Map && q['kind'] == 'questions' && q['questions'] is List && (q['questions'] as List).isNotEmpty) {
      _applyEvent({'event': 'ask', 'id': q['id'], 'mode': 'question', 'kind': 'questions', 'question': q['question'], 'intro': q['intro'], 'questions': q['questions']});
      _current?.done = true;
    }
    final s = raw['suggestions'];
    if (s is List) _applyEvent({'event': 'suggestions', 'items': s});
  }

  void _closeQuestions() {
    for (final it in items) {
      for (final b in it.blocks) {
        if (b.t == 'ask' && b.mode == 'question' && b.answered == null) b.answered = '';
      }
    }
  }

  void _applyEvent(Map<String, dynamic> e) {
    final turn = '${e['turn'] ?? ''}';
    switch ('${e['event']}') {
      case 'user':
        _closeQuestions();
        suggestions = [];
        items.add(ChatItem('user', text: '${e['text'] ?? ''}', ts: _now())..files = ((e['attachments'] as List?) ?? const []).map((x) => x is Map ? '${x['name']}' : '$x').toList());
        break;
      case 'accepted':
      case 'started':
        if (e['event'] == 'accepted') suggestions = [];
        _assistant(turn);
        break;
      case 'woke':
        _note('${e['text'] ?? ''}'.replaceFirst(RegExp(r'^\[planner\]\s*'), ''), turn: turn);
        _assistant(turn);
        break;
      case 'delta':
        _textBlock(e['kind'] == 'reasoning' ? 'reasoning' : 'text', '${e['text'] ?? ''}', turn);
        break;
      case 'mode':
        final m = '${e['mode']}';
        if (info != null) info!.raw['mode'] = m;
        _note('${Texts.t('chat.mode')}: ${Texts.t('mode.$m')}');
        break;
      case 'agent':
        final d = e['data'] is Map ? Map<String, dynamic>.from(e['data'] as Map) : <String, dynamic>{};
        final type = '${d['type']}';
        if (type == 'assistant') {
          final a = _assistant(turn);
          if (!a.blocks.any((b) => b.t == 'text' || b.t == 'reasoning')) {
            if ('${d['reasoning'] ?? ''}'.isNotEmpty) _textBlock('reasoning', '${d['reasoning']}', turn);
            if ('${d['content'] ?? ''}'.isNotEmpty) _textBlock('text', '${d['content']}', turn);
          }
        } else if (type == 'tool_start') {
          if (d['name'] == 'ask_user') break;
          final b = Block('tool', id: '${d['call_id'] ?? ''}', name: '${d['name'] ?? ''}');
          b.args = d['arguments'] is Map ? Map<String, dynamic>.from(d['arguments'] as Map) : {};
          _addBlock(b, turn);
        } else if (type == 'tool_end') {
          if (d['name'] == 'ask_user') break;
          final b = _toolBlock('${d['call_id'] ?? ''}') ?? _addBlock(Block('tool', id: '${d['call_id'] ?? ''}', name: '${d['name'] ?? ''}'), turn);
          b.status = d['status'] == 'completed' ? 'ok' : 'failed';
          final out = '${d['output'] ?? ''}';
          b.output = out.length > shownOutput ? out.substring(0, shownOutput) : out;
        }
        break;
      case 'ask':
      case 'pending':                                           // a question left for the person: answered like any other, whenever
        final b = Block('ask', id: '${e['id'] ?? ''}');
        b.mode = '${e['mode'] ?? ''}';
        final facts = e['facts'] is Map ? Map<String, dynamic>.from(e['facts'] as Map) : <String, dynamic>{};
        b.kind = '${e['kind'] ?? facts['kind'] ?? ''}';
        b.question = '${e['question'] ?? ''}';
        b.facts = facts;
        b.options = ((e['options'] as List?) ?? const []).whereType<Map>().map((o) => Map<String, dynamic>.from(o)).toList();
        b.intro = '${e['intro'] ?? ''}';
        b.questions = ((e['questions'] as List?) ?? const []).whereType<Map>().map((o) => Map<String, dynamic>.from(o)).toList();
        b.name = '${e['tool'] ?? ''}';
        if (e['arguments'] is Map) b.args = Map<String, dynamic>.from(e['arguments'] as Map);
        _addBlock(b, turn);
        break;
      case 'intake': // what the harness assumed or left for the person before the agent began
        final assumed = ((e['assumptions'] as List?) ?? const []).map((x) => '$x').toList();
        final parked = ((e['parked'] as List?) ?? const []).map((x) => '$x').toList();
        final lines = [
          if (assumed.isNotEmpty) Texts.t('chat.intake.assumed', [assumed.join('; ')]),
          if (parked.isNotEmpty) Texts.t('chat.intake.parked', [parked.join('; ')]),
        ];
        if (lines.isNotEmpty) _addBlock(Block('note', s: lines.join(' ')), turn);
        break;
      case 'suggestions':
        suggestions = ((e['items'] as List?) ?? const [])
            .whereType<Map>()
            .map((o) => {'label': '${o['label'] ?? ''}', 'prompt': '${o['prompt'] ?? ''}'})
            .where((o) => o['prompt']!.isNotEmpty)
            .toList();
        break;
      case 'answered':
        for (final it in items) {
          for (final b in it.blocks) {
            if (b.t == 'ask' && b.id == '${e['ask']}') b.answered = '${e['value']}';
          }
        }
        break;
      case 'blocked':
        final b = Block('blocked');
        b.reason = '${e['reason'] ?? ''}';
        b.detail = '${e['detail'] ?? ''}';
        b.options = ((e['options'] as List?) ?? const []).whereType<Map>().map((o) => Map<String, dynamic>.from(o)).toList();
        _addBlock(b, turn);
        break;
      case 'retrying':
        _addBlock(Block('note', s: '${e['attempt']} · ${e['wait_s']} s'), turn);
        break;
      case 'done':
        final a = _assistant(turn);
        a.done = true;
        a.meta = e['stop_reason'] == 'cancelled' ? Texts.t('chat.cancelled') : ('${e['error'] ?? ''}');
        for (final b in a.blocks) {
          if (b.t == 'reasoning') b.over = true;
        }
        if (e['stop_reason'] == 'final' || e['stop_reason'] == null) onAnswerDone?.call(a);
        break;
      default:
        break;
    }
  }

  void _fromHistory(List messages) {
    for (final raw in messages) {
      if (raw is! Map) continue;
      final m = Map<String, dynamic>.from(raw);
      _stamp = (m['ts'] as num?)?.toDouble();
      final role = '${m['role']}';
      if (role == 'user') {
        final text = m['content'] is String ? m['content'] as String : '';
        if (text.startsWith('[harness]')) {
          _assistant('');
          _addBlock(Block('note', s: text.replaceFirst(RegExp(r'^\[harness\]\s*'), '')), '');
        } else {
          items.add(ChatItem('user', text: m['raw'] is String ? m['raw'] as String : text, ts: _now())..files = ((m['attachments'] as List?) ?? const []).map((x) => '$x').toList());
        }
      } else if (role == 'assistant') {
        final content = '${m['content'] ?? ''}';
        if (content.isNotEmpty) _textBlock('text', content, '');
        for (final tc in (m['tool_calls'] as List? ?? const [])) {
          if (tc is! Map) continue;
          final fn = tc['function'] is Map ? Map<String, dynamic>.from(tc['function'] as Map) : <String, dynamic>{};
          if (fn['name'] == 'ask_user') continue;
          final b = Block('tool', id: '${tc['id'] ?? ''}', name: '${fn['name'] ?? ''}')..status = 'ok';
          _addBlock(b, '');
        }
        if (_current != null) _current!.done = true;
      } else if (role == 'tool') {
        final b = _toolBlock('${m['tool_call_id'] ?? ''}');
        if (b != null) {
          final out = '${m['content'] ?? ''}';
          b.output = out.length > shownOutput ? out.substring(0, shownOutput) : out;
        }
      }
      _stamp = null;
    }
    _current = null;
  }

  // ---- talking ----
  Future<void> send(String text, {List<String> attachments = const [], Map<String, dynamic>? place}) async {
    if (sid.isEmpty || text.trim().isEmpty && attachments.isEmpty) return;
    await api.post('/api/agent/sessions/$sid/send', {'text': text, if (attachments.isNotEmpty) 'attachments': attachments, 'place': ?place});
    suggestions = [];
    state = 'busy';
    notifyListeners();
    _schedule(true);
  }

  /// The answer to a card of questions: ids and typed text, which the server checks and puts in words for the agent. `how` is
  /// submitted, skipped or chat (the person writes the answer in the message box instead).
  Future<void> answerCard(Block b, String value, String how) async {
    await api.post('/api/agent/sessions/$sid/answer', {'ask': b.id, 'value': value});
    b.answered = how;
    if (how != 'chat') state = 'busy';
    notifyListeners();
    if (how != 'chat') _schedule(true);
  }

  Future<void> answer(Block b, String value) async {
    await api.post('/api/agent/sessions/$sid/answer', {'ask': b.id, 'value': value});
    b.answered = b.kind == 'code' ? '-' : value;
    state = 'busy';
    notifyListeners();
    _schedule(true);
  }

  Future<void> stop() async {
    if (sid.isEmpty) return;
    await api.post('/api/agent/sessions/$sid/cancel');
  }

  Future<void> setMode(String mode) async {
    final r = await api.post('/api/agent/sessions/$sid/mode', {'mode': mode});
    info = SessionInfo(Map<String, dynamic>.from(r['session'] as Map));
    notifyListeners();
  }

  Future<void> control(String what) async {
    await api.post('/api/agent/sessions/$sid/$what');
    state = what == 'close' ? 'closed' : 'busy';
    notifyListeners();
    _schedule(true);
  }

  /// A new conversation that starts from this answer: how many of the person's requests come before it.
  Future<String> branchFrom(ChatItem answer) async {
    final turn = items.sublist(0, items.indexOf(answer)).where((x) => x.kind == 'user').length;
    final r = await api.post('/api/agent/sessions/$sid/branch', {'turn': turn});
    return '${(r['session'] as Map)['id']}';
  }
}

/// What a new conversation starts with: the same fields the PC's page sends when it makes one.
class NewChat {
  NewChat({this.mode = 'default', this.effort = '', this.allow = const [], this.worktree = 'auto', this.askOnly = false});
  final String mode;

  /// '' (high, the default), 'medium', 'low' or 'none' (no reasoning).
  final String effort;
  final List<String> allow;

  /// 'off', 'auto' or 'always'.
  final String worktree;
  final bool askOnly;

  NewChat copy({String? mode, String? effort, List<String>? allow, String? worktree, bool? askOnly}) =>
      NewChat(mode: mode ?? this.mode, effort: effort ?? this.effort, allow: allow ?? this.allow, worktree: worktree ?? this.worktree, askOnly: askOnly ?? this.askOnly);

  Map<String, dynamic> body() => {'mode': mode, 'allow': allow, 'ask_only': askOnly, 'effort': effort, 'worktree': worktree};
}
