{{/* Labels every object gets: what it is, which release, which chart version. */}}
{{- define "ai-gateway.labels" -}}
app.kubernetes.io/part-of: ai-gateway
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/* What the gateway's Service, Deployment and disruption budget select its pods by. */}}
{{- define "ai-gateway.gatewaySelector" -}}
app.kubernetes.io/name: gateway
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "ai-gateway.postgresSelector" -}}
app.kubernetes.io/name: postgres
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "ai-gateway.llmSelector" -}}
app.kubernetes.io/name: llm
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* The same lock-down for the observability containers, as the user each image is built to run as. */}}
{{- define "ai-gateway.lockedDown" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: {{ .uid }}
  runAsGroup: {{ .uid }}
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: [ALL]
{{- end }}

{{/* The gateway container's locked-down settings, shared with its migration init container. */}}
{{- define "ai-gateway.containerSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: [ALL]
{{- end }}
