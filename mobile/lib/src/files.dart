import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/services.dart';
import 'package:image_picker/image_picker.dart';

/// A file chosen on the phone, ready to be sent to the PC.
class Picked {
  Picked(this.name, this.path);
  final String name;
  final String path;
  Future<Uint8List> bytes() => File(path).readAsBytes();
}

/// The ways the + button reaches a file. Each one uses what the phone already has: its photo picker, its camera, its own document window,
/// and for the PC's folders the same document window pointed at Basalt's entry in it.
class Pickers {
  static const _channel = MethodChannel('app.basalt/picker');
  static final ImagePicker _images = ImagePicker();

  /// Images from the phone, with its own picker.
  static Future<List<Picked>> images() async {
    final got = await _images.pickMultiImage();
    return [for (final x in got) Picked(x.name, x.path)];
  }

  /// A photo taken now with the Camera app.
  static Future<List<Picked>> photo() async {
    final x = await _images.pickImage(source: ImageSource.camera);
    return x == null ? [] : [Picked(x.name, x.path)];
  }

  /// Files from the phone, with the standard Android document window.
  static Future<List<Picked>> local() async {
    final got = await FilePicker.pickFiles();
    return [for (final f in got) if (f.path != null) Picked(f.name, f.path!)];
  }

  /// Files from the PC: the standard document window, opened on Basalt's entry (the PC's shared folders).
  static Future<List<Picked>> server() async => _documents(true);

  static Future<List<Picked>> _documents(bool basalt) async {
    final got = await _channel.invokeMethod<List<dynamic>>('pickDocuments', {'basalt': basalt});
    return [for (final x in got ?? const []) Picked('${(x as Map)['name']}', '${x['path']}')];
  }

  /// Saves bytes where the person chooses, with the standard "Save as" window.
  static Future<bool> save(String name, List<int> bytes) async => (await FilePicker.saveFile(fileName: name, bytes: Uint8List.fromList(bytes))) != null;
}
