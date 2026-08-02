{{/*
Common labels for a surface. Call with a dict: (dict "root" $root "surface" $name)
*/}}
{{- define "workload.labels" -}}
app.kubernetes.io/name: {{ .root.Values.app }}
app.kubernetes.io/component: {{ .surface }}
app.kubernetes.io/part-of: {{ .root.Values.app }}
app.kubernetes.io/managed-by: workload-chart
{{- end -}}

{{/*
Namespace: explicit override or the app name.
*/}}
{{- define "workload.namespace" -}}
{{- .Values.namespace | default .Values.app -}}
{{- end -}}

{{/*
Managed secret name: explicit override or "<app>-secret".
*/}}
{{- define "workload.secretName" -}}
{{- .Values.secret.managedSecretName | default (printf "%s-secret" .Values.app) -}}
{{- end -}}

{{/*
Fail fast on the two required values.
*/}}
{{- define "workload.validate" -}}
{{- if not .Values.app -}}
{{- fail "values.app is required" -}}
{{- end -}}
{{- range $name, $surface := .Values.surfaces -}}
{{- if not $surface.image -}}{{- fail (printf "surface %q: image is required" $name) -}}{{- end -}}
{{- if not $surface.port -}}{{- fail (printf "surface %q: port is required" $name) -}}{{- end -}}
{{- end -}}
{{- end -}}
