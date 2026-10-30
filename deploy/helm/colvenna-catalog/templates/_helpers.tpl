{{- define "colvenna-catalog.labels" -}}
app.kubernetes.io/name: colvenna-catalog
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: colvenna-catalog
{{- end }}

{{- define "colvenna-catalog.selector" -}}
app.kubernetes.io/name: colvenna-catalog
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "colvenna-catalog.pairs" -}}
{{- $pairs := list -}}
{{- range $k, $v := . -}}
{{- $pairs = append $pairs (printf "%s=%s" $k $v) -}}
{{- end -}}
{{- join "," $pairs -}}
{{- end }}

{{- /*
The internal NLB's sources, checked: allowedCidrs (its clients) must be listed, each a well-formed
IPv4 or IPv6 CIDR with a prefix longer than 0; healthCheckCidrs (the NLB's own addresses, which its
health checks come from) must each be one address (/32 or /128). Returns the allowed CIDRs as JSON.
*/ -}}
{{- define "colvenna-catalog.cidr" -}}
{{- $ipv4 := "^([0-9]{1,3}[.]){3}[0-9]{1,3}/([1-9]|[12][0-9]|3[0-2])$" -}}
{{- $ipv6 := "^[0-9a-fA-F]*:[0-9a-fA-F:]*/([1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])$" -}}
{{- if not (or (regexMatch $ipv4 (toString .cidr)) (regexMatch $ipv6 (toString .cidr))) -}}
{{- fail (printf "internalLoadBalancer.%s: %q is not a CIDR with a prefix longer than 0" .key (toString .cidr)) -}}
{{- end -}}
{{- end }}

{{- /*
A value of shutdown.* (#64), checked: a whole number of seconds, 0 or more; a typo fails the render
rather than become 0. Returns it.
*/ -}}
{{- define "colvenna-catalog.seconds" -}}
{{- $value := index .Values.shutdown .key -}}
{{- if not (regexMatch "^[0-9]+$" (toString $value)) -}}
{{- fail (printf "shutdown.%s: %q is not a whole number of seconds" .key (toString $value)) -}}
{{- end -}}
{{- int $value -}}
{{- end }}

{{- define "colvenna-catalog.nlbCidrs" -}}
{{- $allowed := .Values.internalLoadBalancer.allowedCidrs | default list -}}
{{- if eq (len $allowed) 0 -}}
{{- fail "internalLoadBalancer.allowedCidrs: list the CIDRs that may reach the internal NLB" -}}
{{- end -}}
{{- range $allowed -}}
{{- include "colvenna-catalog.cidr" (dict "key" "allowedCidrs" "cidr" .) -}}
{{- end -}}
{{- range (.Values.internalLoadBalancer.healthCheckCidrs | default list) -}}
{{- include "colvenna-catalog.cidr" (dict "key" "healthCheckCidrs" "cidr" .) -}}
{{- if not (or (hasSuffix "/32" (toString .)) (hasSuffix "/128" (toString .))) -}}
{{- fail (printf "internalLoadBalancer.healthCheckCidrs: %s is not one address (/32 or /128)" .) -}}
{{- end -}}
{{- end -}}
{{- toJson $allowed -}}
{{- end }}
