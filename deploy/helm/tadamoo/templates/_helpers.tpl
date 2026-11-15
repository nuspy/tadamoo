{{- define "tadamoo.harness" -}}{{ .Release.Name }}-harness{{- end -}}
{{- define "tadamoo.engine" -}}{{ .Release.Name }}-engine{{- end -}}
{{- define "tadamoo.secret" -}}{{ default (printf "%s-key" .Release.Name) .Values.harness.existingSecret }}{{- end -}}
{{- define "tadamoo.hosts" -}}
{{- $h := list -}}
{{- if .Values.ingress.host }}{{ $h = append $h .Values.ingress.host }}{{ end -}}
{{- range .Values.allowedHosts }}{{ $h = append $h . }}{{ end -}}
{{- $h | uniq | toJson -}}
{{- end -}}
