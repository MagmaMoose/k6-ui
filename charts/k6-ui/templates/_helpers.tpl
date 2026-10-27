{{- define "k6-ui.fullname" -}}
{{- default (printf "%s-k6-ui" .Release.Name) .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "k6-ui.labels" -}}
app.kubernetes.io/name: k6-ui
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}
{{- define "k6-ui.selectorLabels" -}}
app.kubernetes.io/name: k6-ui
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: web
{{- end -}}
{{- define "k6-ui.serviceAccountName" -}}
{{- default (include "k6-ui.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}
{{- define "k6-ui.sessionSecret" -}}
{{- default (printf "%s-session" (include "k6-ui.fullname" .)) .Values.session.existingSecret -}}
{{- end -}}
{{- define "k6-ui.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ default .Chart.AppVersion .Values.image.tag }}
{{- end -}}
{{- end -}}
