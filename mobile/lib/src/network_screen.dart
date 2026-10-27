import 'dart:io';

import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:smb_connect/smb_connect.dart';

import 'api.dart';
import 'files.dart';
import 'i18n.dart';

/// Files from other devices on the network: the PC lists the devices Tailscale sees (or one is typed), the phone signs in to the one chosen
/// over SMB, the shared folders are walked here, and the files marked are copied to the phone to be attached.
class NetworkScreen extends StatefulWidget {
  const NetworkScreen({super.key, required this.api});
  final Api api;

  @override
  State<NetworkScreen> createState() => _NetworkScreenState();
}

class _NetworkScreenState extends State<NetworkScreen> {
  List<Map<String, dynamic>> peers = [];
  final host = TextEditingController();
  SmbConnect? connection;
  final List<SmbFile> trail = [];
  List<SmbFile> entries = [];
  final Map<String, SmbFile> marked = {};
  bool loading = false;
  String? problem;

  @override
  void initState() {
    super.initState();
    _peers();
  }

  @override
  void dispose() {
    host.dispose();
    connection?.close();
    super.dispose();
  }

  Future<void> _peers() async {
    try {
      final r = await widget.api.get('/api/remote/peers');
      if (!mounted) return;
      setState(() => peers = ((r['peers'] as List?) ?? const []).whereType<Map>().map((p) => Map<String, dynamic>.from(p)).toList());
    } on ApiError catch (e) {
      if (mounted) setState(() => problem = e.message);
    }
  }

  Future<void> _sign(String address) async {
    final user = TextEditingController(), pass = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(address),
        content: Column(mainAxisSize: MainAxisSize.min, children: [
          TextField(controller: user, decoration: InputDecoration(labelText: Texts.t('net.user')), autocorrect: false),
          TextField(controller: pass, decoration: InputDecoration(labelText: Texts.t('net.pass')), obscureText: true),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(Texts.t('conv.cancel'))),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(Texts.t('net.connect'))),
        ],
      ),
    );
    if (ok != true) return;
    setState(() {
      loading = true;
      problem = null;
    });
    try {
      final c = await SmbConnect.connectAuth(host: address, domain: '', username: user.text.trim(), password: pass.text);
      connection = c;
      trail.clear();
      entries = await c.listShares();
    } catch (e) {
      problem = Texts.t('net.fail', ['$e']);
      connection = null;
    }
    if (mounted) setState(() => loading = false);
  }

  Future<void> _open(SmbFile f) async {
    setState(() => loading = true);
    try {
      final list = await connection!.listFiles(f);
      trail.add(f);
      entries = list.where((e) => e.name != '.' && e.name != '..').toList();
    } catch (e) {
      problem = Texts.t('net.fail', ['$e']);
    }
    if (mounted) setState(() => loading = false);
  }

  Future<void> _up() async {
    if (trail.isEmpty) {
      connection?.close();
      setState(() {
        connection = null;
        entries = [];
        marked.clear();
      });
      return;
    }
    trail.removeLast();
    setState(() => loading = true);
    try {
      entries = trail.isEmpty ? await connection!.listShares() : (await connection!.listFiles(trail.last)).where((e) => e.name != '.' && e.name != '..').toList();
    } catch (e) {
      problem = Texts.t('net.fail', ['$e']);
    }
    if (mounted) setState(() => loading = false);
  }

  Future<void> _choose() async {
    setState(() => loading = true);
    final out = <Picked>[];
    try {
      final dir = await getTemporaryDirectory();
      for (final f in marked.values) {
        final target = File('${dir.path}${Platform.pathSeparator}${DateTime.now().microsecondsSinceEpoch}_${f.name}');
        final sink = target.openWrite();
        final stream = await connection!.openRead(f);
        await sink.addStream(stream);
        await sink.close();
        out.add(Picked(f.name, target.path));
      }
    } catch (e) {
      problem = Texts.t('net.fail', ['$e']);
      if (mounted) setState(() => loading = false);
      return;
    }
    if (mounted) Navigator.pop(context, out);
  }

  @override
  Widget build(BuildContext context) {
    final inside = connection != null;
    return Scaffold(
      appBar: AppBar(
        title: Text(inside ? (trail.isEmpty ? Texts.t('net.shares') : trail.last.name) : Texts.t('net.title')),
        leading: inside ? IconButton(icon: const Icon(Icons.arrow_back), onPressed: _up) : null,
        actions: [if (inside && marked.isNotEmpty) TextButton(onPressed: loading ? null : _choose, child: Text('${Texts.t('net.pickhere')} (${marked.length})'))],
      ),
      body: Column(children: [
        if (loading) const LinearProgressIndicator(),
        if (problem != null) MaterialBanner(content: Text(problem!), actions: [TextButton(onPressed: () => setState(() => problem = null), child: const Text('OK'))]),
        Expanded(child: inside ? _listing() : _devices()),
      ]),
    );
  }

  Widget _devices() {
    return ListView(children: [
      Padding(
        padding: const EdgeInsets.all(12),
        child: Row(children: [
          Expanded(child: TextField(controller: host, decoration: InputDecoration(labelText: Texts.t('net.host'), border: const OutlineInputBorder(), isDense: true), autocorrect: false)),
          const SizedBox(width: 8),
          FilledButton(onPressed: () => host.text.trim().isEmpty ? null : _sign(host.text.trim()), child: Text(Texts.t('net.connect'))),
        ]),
      ),
      if (peers.isEmpty) Padding(padding: const EdgeInsets.all(16), child: Text(Texts.t('net.empty'))),
      for (final p in peers)
        ListTile(
          leading: Icon(p['online'] == true ? Icons.devices : Icons.devices_other, color: p['online'] == true ? null : Colors.white38),
          title: Text('${p['name']}'),
          subtitle: Text('${p['ip']} · ${p['os']} · ${Texts.t(p['online'] == true ? 'net.online' : 'net.offline')}'),
          onTap: () => _sign('${p['ip']}'),
        ),
    ]);
  }

  Widget _listing() {
    return ListView(children: [
      for (final f in entries)
        f.isDirectory() || trail.isEmpty
            ? ListTile(leading: const Icon(Icons.folder_outlined), title: Text(f.name), onTap: () => _open(f))
            : CheckboxListTile(
                value: marked.containsKey(f.path),
                title: Text(f.name),
                subtitle: Text('${f.size} B'),
                secondary: const Icon(Icons.insert_drive_file_outlined),
                onChanged: (v) => setState(() => v == true ? marked[f.path] = f : marked.remove(f.path)),
              ),
    ]);
  }
}
