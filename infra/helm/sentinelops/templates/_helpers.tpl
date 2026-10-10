{{- define "sentinelops.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "sentinelops.labels" -}}
app.kubernetes.io/part-of: sentinelops
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "sentinelops.selector" -}}
app.kubernetes.io/part-of: sentinelops
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .name }}
{{- end -}}

{{- define "sentinelops.platformImage" -}}
{{ .Values.image.platform.repository }}:{{ .Values.image.platform.tag | default .Chart.AppVersion }}
{{- end -}}

{{/* ServiceAccount for a kube role: "" -> no-token default, reader, executor. */}}
{{- define "sentinelops.serviceAccount" -}}
{{- if .kube -}}{{ include "sentinelops.fullname" .root }}-{{ .kube }}
{{- else -}}{{ include "sentinelops.fullname" .root }}{{- end -}}
{{- end -}}

{{/* Env shared by every Python service. */}}
{{- define "sentinelops.commonEnv" -}}
- name: DATABASE_URL
  valueFrom: { secretKeyRef: { name: {{ .Values.existingSecret }}, key: database-url } }
- name: KAFKA_BOOTSTRAP
  value: {{ .Values.config.kafkaBootstrap | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.config.otlpEndpoint | quote }}
- name: OTEL_METRIC_EXPORT_INTERVAL
  value: "10000"
- name: OTEL_SEMCONV_STABILITY_OPT_IN
  value: http
- name: DEPLOYMENT_ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
- name: LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: SENTINEL_TARGET_NAMESPACE
  value: {{ .Values.policy.namespace | quote }}
- name: SENTINEL_ALLOWED_DEPLOYMENTS
  value: {{ join "," .Values.policy.deployments | quote }}
- name: SENTINEL_ALLOWED_FLAGS
  value: {{ join "," .Values.policy.featureFlags | quote }}
- name: SENTINEL_SCALE_MAX
  value: {{ .Values.policy.scaleMax | toString | quote }}
{{- end -}}

{{- define "sentinelops.secretEnvName" -}}
{{ . | upper | replace "-" "_" }}_FILE
{{- end -}}
