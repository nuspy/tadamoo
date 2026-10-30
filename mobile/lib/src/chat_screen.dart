import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'answer_view.dart';
import 'api.dart';
import 'ask_card.dart';
import 'chat_model.dart';
import 'files.dart';
import 'i18n.dart';
import 'network_screen.dart';
import 'place.dart';
import 'settings.dart';
import 'voice.dart';

/// One conversation: the messages, what the agent is doing, its questions with their buttons, and the box to write in.
class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, required this.api, required this.settings, required this.voice, this.sessionId, this.workspace = '', this.options});
  final Api api;
  final AppSettings settings;
  final Voice voice;
  final String? sessionId;
  final String workspace;
  final NewChat? options;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _Attached {
  _Attached(this.id, this.name);
  final String id;
  final String name;
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  late final ChatController ctl;
  final input = TextEditingController();
  final inputFocus = FocusNode();
  final scroll = ScrollController();
  final List<_Attached> attached = [];
  String uploading = '';
  bool stickToEnd = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    ctl = ChatController(widget.api, widget.settings)
      ..onAnswerDone = (a) {
        if (widget.settings.autoSpeak) widget.voice.speak(a.answerText);
      };
    ctl.addListener(_changed);
    widget.voice.addListener(_changed);
    scroll.addListener(() {
      if (scroll.hasClients) stickToEnd = scroll.position.maxScrollExtent - scroll.offset < 160;
    });
    if (widget.sessionId != null) ctl.open(widget.sessionId!);
    input.addListener(_warmPlace);
  }

  /// A reading is taken while the person writes, so sending does not wait for it.
  void _warmPlace() {
    if (input.text.isNotEmpty) PlaceFinder.of(widget.settings).current();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      ctl.resume();
    } else {
      ctl.pause();
    }
  }

  void _changed() {
    if (!mounted) return;
    setState(() {});
    if (stickToEnd) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (scroll.hasClients) scroll.jumpTo(scroll.position.maxScrollExtent);
      });
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    ctl.removeListener(_changed);
    widget.voice.removeListener(_changed);
    ctl.dispose();
    input.dispose();
    inputFocus.dispose();
    scroll.dispose();
    super.dispose();
  }

  void _say(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  Future<bool> _ensure() async {
    if (ctl.ready) return true;
    try {
      await ctl.create(workspace: widget.workspace, options: widget.options ?? NewChat(mode: widget.settings.mode));
      return true;
    } on ApiError catch (e) {
      _say(e.message);
      return false;
    }
  }

  Future<void> _send() async {
    final text = input.text.trim();
    if (text.isEmpty && attached.isEmpty) return;
    if (!await _ensure()) return;
    final ids = attached.map((a) => a.id).toList();
    try {
      await ctl.send(text, attachments: ids, place: await PlaceFinder.of(widget.settings).current());
      input.clear();
      setState(() => attached.clear());
      stickToEnd = true;
    } on ApiError catch (e) {
      _say(e.message);
    }
  }

  // ---- the + button ----
  Future<void> _plus() async {
    final choice = await showModalBottomSheet<String>(
      context: context,
      builder: (c) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(leading: const Icon(Icons.image_outlined), title: Text(Texts.t('plus.image')), onTap: () => Navigator.pop(c, 'image')),
          ListTile(leading: const Icon(Icons.photo_camera_outlined), title: Text(Texts.t('plus.photo')), onTap: () => Navigator.pop(c, 'photo')),
          ListTile(leading: const Icon(Icons.folder_open), title: Text(Texts.t('plus.files')), trailing: const Icon(Icons.chevron_right), onTap: () => Navigator.pop(c, 'files')),
        ]),
      ),
    );
    if (choice == null || !mounted) return;
    if (choice == 'image') return _attach(Pickers.images);
    if (choice == 'photo') return _attach(Pickers.photo);
    final where = await showModalBottomSheet<String>(
      context: context,
      builder: (c) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(leading: const Icon(Icons.smartphone), title: Text(Texts.t('plus.local')), onTap: () => Navigator.pop(c, 'local')),
          ListTile(leading: const Icon(Icons.computer), title: Text(Texts.t('plus.server')), onTap: () => Navigator.pop(c, 'server')),
          ListTile(leading: const Icon(Icons.lan_outlined), title: Text(Texts.t('plus.network')), onTap: () => Navigator.pop(c, 'network')),
        ]),
      ),
    );
    if (where == null || !mounted) return;
    if (where == 'local') return _attach(Pickers.local);
    if (where == 'server') return _attach(Pickers.server);
    if (where == 'network') {
      final nav = Navigator.of(context);
      final got = await nav.push<List<Picked>>(MaterialPageRoute(builder: (_) => NetworkScreen(api: widget.api)));
      if (got != null && got.isNotEmpty) await _upload(got);
    }
  }

  Future<void> _attach(Future<List<Picked>> Function() pick) async {
    List<Picked> got;
    try {
      got = await pick();
    } catch (e) {
      _say('$e');
      return;
    }
    if (got.isNotEmpty) await _upload(got);
  }

  Future<void> _upload(List<Picked> files) async {
    if (!await _ensure()) return;
    for (final f in files) {
      setState(() => uploading = f.name);
      try {
        final meta = await widget.api.upload(ctl.sid, f.name, await f.bytes());
        if (!mounted) return;
        setState(() => attached.add(_Attached('${meta['id']}', '${meta['name'] ?? f.name}')));
      } on ApiError catch (e) {
        _say(Texts.t('plus.fail', [f.name, e.message]));
      } catch (e) {
        _say(Texts.t('plus.fail', [f.name, '$e']));
      }
    }
    if (mounted) setState(() => uploading = '');
  }

  // ---- the microphone ----
  Future<void> _mic() async {
    final voice = widget.voice;
    if (voice.listening) {
      await voice.stopListening();
      return;
    }
    final before = input.text.isEmpty ? '' : '${input.text.trimRight()} ';
    final ok = await voice.listen((words, done) {
      input.value = TextEditingValue(text: before + words, selection: TextSelection.collapsed(offset: (before + words).length));
    });
    if (!ok) _say(voice.problem ?? 'speech');
  }

  // ---- files named in an answer ----
  Future<void> _saveFile(String kind, String ref, String name) async {
    try {
      final got = kind == 'file'
          ? await widget.api.download('/api/outputs/${Uri.encodeComponent(ctl.sid)}/$ref')
          : await widget.api.download('/api/agent/sessions/${Uri.encodeComponent(ctl.sid)}/file', {'path': ref});
      final fileName = name.contains('.') || got.name == 'file' ? name : got.name;
      final saved = await Pickers.save(got.name.contains('.') ? got.name : fileName, got.bytes);
      if (saved) _say(Texts.t('chat.saved', [got.name]));
    } on ApiError catch (e) {
      _say(Texts.t('chat.savefail', [name, e.message]));
    } catch (e) {
      _say(Texts.t('chat.savefail', [name, '$e']));
    }
  }

  Future<void> _copy(String text) async {
    await Clipboard.setData(ClipboardData(text: text));
    _say(Texts.t('chat.copied'));
  }

  Future<void> _branch(ChatItem a) async {
    try {
      final id = await ctl.branchFrom(a);
      if (!mounted) return;
      _say(Texts.t('chat.branched'));
      await Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => ChatScreen(api: widget.api, settings: widget.settings, voice: widget.voice, sessionId: id)));
    } on ApiError catch (e) {
      _say(e.message);
    }
  }

  // ---- the mode ----
  Future<void> _modeSheet() async {
    if (!ctl.ready) return;
    final now = ctl.info?.mode ?? 'default';
    final picked = await showModalBottomSheet<String>(
      context: context,
      builder: (c) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          for (final m in const ['plan', 'read_only', 'default', 'auto'])
            ListTile(
              leading: Icon(m == now ? Icons.radio_button_checked : Icons.radio_button_off),
              title: Text(Texts.t('mode.$m')),
              subtitle: Text(Texts.t('mode.$m.d')),
              onTap: () => Navigator.pop(c, m),
            ),
        ]),
      ),
    );
    if (picked == null || picked == now) return;
    try {
      await ctl.setMode(picked);
    } on ApiError catch (e) {
      _say(e.message);
    }
  }

  // ---- drawing ----
  @override
  Widget build(BuildContext context) {
    final mode = ctl.info?.mode ?? widget.settings.mode;
    return Scaffold(
      appBar: AppBar(
        title: Text(ctl.info == null || ctl.info!.title.isEmpty ? Texts.t('conv.untitled') : ctl.info!.title, maxLines: 1, overflow: TextOverflow.ellipsis),
        actions: [
          if (widget.voice.speaking) IconButton(icon: const Icon(Icons.volume_off), onPressed: widget.voice.stopSpeaking),
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: ActionChip(label: Text(Texts.t('mode.$mode')), onPressed: _modeSheet),
          ),
        ],
      ),
      body: Column(children: [
        if (ctl.error != null) MaterialBanner(content: Text(ctl.error!), actions: [TextButton(onPressed: () => ctl.sid.isEmpty ? null : ctl.open(ctl.sid), child: Text(Texts.t('conv.retry')))]),
        Expanded(
          child: ctl.items.isEmpty
              ? Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(Texts.t('chat.empty'), textAlign: TextAlign.center)))
              : ListView.builder(
                  controller: scroll,
                  padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
                  itemCount: ctl.items.length + (ctl.busy ? 1 : 0),
                  itemBuilder: (c, i) {
                    if (i >= ctl.items.length) return Padding(padding: const EdgeInsets.all(12), child: Row(children: [const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)), const SizedBox(width: 10), Text(Texts.t('chat.working'))]));
                    return _item(ctl.items[i]);
                  },
                ),
        ),
        _composer(),
      ]),
    );
  }

  Widget _item(ChatItem it) {
    final theme = Theme.of(context);
    if (it.kind == 'note') {
      return Padding(padding: const EdgeInsets.symmetric(vertical: 4), child: Center(child: Text(it.text, style: theme.textTheme.bodySmall, textAlign: TextAlign.center)));
    }
    if (it.kind == 'user') {
      return Align(
        alignment: Alignment.centerRight,
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 6),
          child: Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
            Container(
              constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.86),
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(color: theme.colorScheme.primaryContainer, borderRadius: BorderRadius.circular(16)),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                if (it.files.isNotEmpty) Padding(padding: const EdgeInsets.only(bottom: 4), child: Text(it.files.join(' · '), style: theme.textTheme.bodySmall)),
                SelectableText(it.text),
              ]),
            ),
            Row(mainAxisSize: MainAxisSize.min, children: [
              _stamp(it.ts),
              IconButton(tooltip: Texts.t('chat.copy'), visualDensity: VisualDensity.compact, iconSize: 18, icon: const Icon(Icons.copy_outlined), onPressed: () => _copy(it.text)),
            ]),
          ]),
        ),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        for (final b in it.blocks) _block(b),
        if (it.done && it.answerText.isNotEmpty)
          Row(children: [
            _stamp(it.ts),
            IconButton(tooltip: Texts.t('chat.copy'), visualDensity: VisualDensity.compact, iconSize: 18, icon: const Icon(Icons.copy_outlined), onPressed: () => _copy(it.answerText)),
            IconButton(tooltip: Texts.t('chat.speak'), visualDensity: VisualDensity.compact, iconSize: 18, icon: const Icon(Icons.volume_up_outlined), onPressed: () => widget.voice.speak(it.answerText)),
            TextButton.icon(onPressed: ctl.busy ? null : () => _branch(it), icon: const Icon(Icons.call_split, size: 18), label: Text(Texts.t('chat.branch'))),
          ]),
        if (it.meta.isNotEmpty) Text(it.meta, style: theme.textTheme.bodySmall),
      ]),
    );
  }

  Widget _stamp(double ts) {
    if (ts <= 0) return const SizedBox.shrink();
    final d = DateTime.fromMillisecondsSinceEpoch((ts * 1000).round());
    final hh = d.hour.toString().padLeft(2, '0'), mm = d.minute.toString().padLeft(2, '0');
    return Padding(padding: const EdgeInsets.symmetric(horizontal: 6), child: Text('$hh:$mm', style: Theme.of(context).textTheme.bodySmall));
  }

  Widget _block(Block b) {
    final theme = Theme.of(context);
    switch (b.t) {
      case 'text':
        return AnswerView(text: b.s, api: widget.api, sid: ctl.sid, onFile: _saveFile);
      case 'reasoning':
        if (b.s.trim().isEmpty) return const SizedBox.shrink();
        return Card(
          margin: const EdgeInsets.symmetric(vertical: 4),
          child: ExpansionTile(
            dense: true,
            leading: const Icon(Icons.psychology_outlined, size: 20),
            title: Text(Texts.t('chat.reasoning'), style: theme.textTheme.bodyMedium),
            children: [
              Padding(padding: const EdgeInsets.fromLTRB(14, 0, 14, 4), child: SelectableText(b.s, style: theme.textTheme.bodySmall)),
              Align(alignment: Alignment.centerRight, child: IconButton(iconSize: 18, tooltip: Texts.t('chat.copy'), icon: const Icon(Icons.copy_outlined), onPressed: () => _copy(b.s))),
            ],
          ),
        );
      case 'tool':
        return _tool(b);
      case 'ask':
        return _ask(b);
      case 'blocked':
        return Card(
          color: theme.colorScheme.errorContainer,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(b.detail.isEmpty ? b.reason : b.detail),
              Row(children: [TextButton(onPressed: () => ctl.control('resume'), child: Text(Texts.t('chat.resume'))), TextButton(onPressed: () => ctl.control('close'), child: Text(Texts.t('chat.close')))]),
            ]),
          ),
        );
      default:
        return Padding(padding: const EdgeInsets.symmetric(vertical: 2), child: Text(b.s, style: theme.textTheme.bodySmall));
    }
  }

  Widget _tool(Block b) {
    final theme = Theme.of(context);
    final made = RegExp(r'^\[file:([a-f0-9]{12}):([^\]]{1,120})\]').firstMatch(b.output);
    final wrote = (b.name == 'write_file' || b.name == 'edit_file') && b.status == 'ok' && b.args['path'] is String ? b.args['path'] as String : '';
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 3),
      child: ExpansionTile(
        dense: true,
        leading: Icon(b.status == 'failed' ? Icons.error_outline : b.status == 'ok' ? Icons.check_circle_outline : Icons.hourglass_empty, size: 18),
        title: Text('${b.name} · ${Texts.t('chat.tool.${b.status}')}', style: theme.textTheme.bodySmall),
        children: [
          if (made != null) Align(alignment: Alignment.centerLeft, child: TextButton.icon(onPressed: () => _saveFile('file', made.group(1)!, made.group(2)!), icon: const Icon(Icons.download), label: Text(Texts.t('chat.save', [made.group(2)])))),
          if (wrote.isNotEmpty) Align(alignment: Alignment.centerLeft, child: TextButton.icon(onPressed: () => _saveFile('path', wrote, wrote.split(RegExp(r'[\\/]')).last), icon: const Icon(Icons.download), label: Text(Texts.t('chat.save', [wrote.split(RegExp(r'[\\/]')).last])))),
          if (b.args.isNotEmpty) Padding(padding: const EdgeInsets.fromLTRB(14, 0, 14, 6), child: SelectableText(b.args.toString(), style: const TextStyle(fontFamily: 'monospace', fontSize: 12))),
          if (b.output.isNotEmpty) Padding(padding: const EdgeInsets.fromLTRB(14, 0, 14, 10), child: SelectableText(b.output, style: const TextStyle(fontFamily: 'monospace', fontSize: 12))),
        ],
      ),
    );
  }

  Widget _ask(Block b) {
    final theme = Theme.of(context);
    final done = b.answered != null;
    if (b.kind == 'code') return _CodeCard(key: ValueKey('code-${b.id}'), question: b.question, done: done, onSend: (code) => ctl.answer(b, code));
    if (b.kind == 'questions') {
      return QuestionsCard(
        key: ValueKey('ask-${b.id}'),
        questions: b.questions,
        intro: b.intro,
        answered: b.answered,
        onSend: (value, how) async {
          try {
            await ctl.answerCard(b, value, how);
            if (how == 'chat') inputFocus.requestFocus();
          } on ApiError catch (e) {
            _say(e.message);
          }
        },
      );
    }
    String label(Map<String, dynamic> o) {
      final v = '${o['value']}';
      if (b.mode == 'confirm' && b.kind != 'parked_question') {
        if (v == 'always') return Texts.t('chat.always', ['${o['rule'] ?? ''}']);
        if (v == 'chat') return Texts.t('chat.chat', ['${o['rule'] ?? ''}']);
        return Texts.t(v == 'confirm' ? (b.kind == 'permission' ? 'chat.once' : 'chat.yes') : 'chat.no');
      }
      return '${o['label'] ?? v}';
    }

    String question = b.question;
    if (b.kind == 'permission') question = '${b.question}\n${b.name} ${b.args.toString().length > 200 ? b.args.toString().substring(0, 200) : b.args}';
    return Card(
      color: theme.colorScheme.secondaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(question),
          const SizedBox(height: 8),
          Wrap(spacing: 8, runSpacing: 4, children: [
            for (final o in b.options)
              (b.mode == 'confirm' && o['value'] == 'confirm'
                  ? FilledButton(onPressed: done ? null : () => ctl.answer(b, '${o['value']}'), child: Text(label(o)))
                  : OutlinedButton(onPressed: done ? null : () => ctl.answer(b, '${o['value']}'), child: Text(label(o)))),
          ]),
          if (done && (b.answered ?? '').isNotEmpty) Padding(padding: const EdgeInsets.only(top: 6), child: Text(Texts.t('chat.answered', [b.answered]), style: theme.textTheme.bodySmall)),
        ]),
      ),
    );
  }

  Widget _composer() {
    final voice = widget.voice;
    return SafeArea(
      top: false,
      child: Container(
        decoration: BoxDecoration(border: Border(top: BorderSide(color: Theme.of(context).dividerColor))),
        padding: const EdgeInsets.fromLTRB(6, 6, 6, 6),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          if (attached.isNotEmpty || uploading.isNotEmpty)
            SizedBox(
              height: 40,
              child: ListView(scrollDirection: Axis.horizontal, children: [
                for (final a in attached) Padding(padding: const EdgeInsets.only(right: 6), child: InputChip(label: Text(a.name, overflow: TextOverflow.ellipsis), onDeleted: () => setState(() => attached.remove(a)))),
                if (uploading.isNotEmpty) Padding(padding: const EdgeInsets.only(right: 6), child: Chip(label: Text(Texts.t('plus.uploading', [uploading])))),
              ]),
            ),
          SuggestionChips(
            items: ctl.suggestions,
            onPick: (p) {
              input.text = p;
              input.selection = TextSelection.collapsed(offset: p.length);
              inputFocus.requestFocus();
            },
          ),
          Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
            IconButton(tooltip: '+', icon: const Icon(Icons.add_circle_outline), onPressed: _plus),
            Expanded(
              child: TextField(
                controller: input,
                focusNode: inputFocus,
                minLines: 1,
                maxLines: 6,
                textCapitalization: TextCapitalization.sentences,
                decoration: InputDecoration(hintText: voice.listening ? Texts.t('chat.hint.listen') : Texts.t('chat.hint'), border: const OutlineInputBorder(), isDense: true),
              ),
            ),
            IconButton(tooltip: 'mic', icon: Icon(voice.listening ? Icons.mic : Icons.mic_none, color: voice.listening ? Colors.redAccent : null), onPressed: _mic),
            ctl.busy
                ? IconButton(tooltip: Texts.t('chat.stop'), icon: const Icon(Icons.stop_circle_outlined), onPressed: ctl.stop)
                : IconButton(tooltip: Texts.t('chat.send'), icon: const Icon(Icons.send), onPressed: _send),
          ]),
        ]),
      ),
    );
  }
}


/// A second-factor code: typed here, it goes to the tool and nowhere else. A card of its own so what is typed stays while the page redraws.
class _CodeCard extends StatefulWidget {
  const _CodeCard({super.key, required this.question, required this.done, required this.onSend});
  final String question;
  final bool done;
  final void Function(String code) onSend;
  @override
  State<_CodeCard> createState() => _CodeCardState();
}

class _CodeCardState extends State<_CodeCard> {
  final code = TextEditingController();
  @override
  void dispose() {
    code.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(widget.question),
          const SizedBox(height: 8),
          Row(children: [
            Expanded(child: TextField(controller: code, obscureText: true, enabled: !widget.done, keyboardType: TextInputType.number, decoration: const InputDecoration(isDense: true, border: OutlineInputBorder()))),
            const SizedBox(width: 8),
            FilledButton(onPressed: widget.done ? null : () => widget.onSend(code.text.trim()), child: Text(Texts.t('chat.send'))),
          ]),
        ]),
      ),
    );
  }
}
