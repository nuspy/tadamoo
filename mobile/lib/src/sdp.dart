/// Puts H.264 first among the video codecs of an SDP offer: the phone has a hardware decoder for it, and the PC sends it.
String preferH264(String sdp) {
  final eol = sdp.contains('\r\n') ? '\r\n' : '\n';
  final lines = sdp.split(eol);
  final h264 = <String>{};
  for (final l in lines) {
    final m = RegExp(r'^a=rtpmap:(\d+) H264/').firstMatch(l);
    if (m != null) h264.add(m.group(1)!);
  }
  if (h264.isEmpty) return sdp;
  for (var i = 0; i < lines.length; i++) {
    if (!lines[i].startsWith('m=video ')) continue;
    final parts = lines[i].split(' ');
    if (parts.length < 5) continue;
    final head = parts.sublist(0, 3);
    final payloads = parts.sublist(3);
    final first = payloads.where(h264.contains).toList();
    final rest = payloads.where((p) => !h264.contains(p)).toList();
    lines[i] = [...head, ...first, ...rest].join(' ');
  }
  return lines.join(eol);
}
