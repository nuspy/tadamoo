# ONNX Runtime's native library finds its Java classes, fields and methods by name: shrinking or renaming them leaves the avatar models without a runtime.
-keep class ai.onnxruntime.** { *; }
-dontwarn ai.onnxruntime.**
