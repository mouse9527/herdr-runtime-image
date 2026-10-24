# herdr-runtime (k8s#64 AC6): SSH login shells get the Pod's LiteLLM key from the mounted Secret file, like the
# processes the supervisor starts. Never passed through the sshd config.
_herdr_key_file=${LITELLM_KEY_FILE:-/etc/herdr-runtime/litellm/api-key}
if [ -r "$_herdr_key_file" ] && [ -s "$_herdr_key_file" ]; then
  LITELLM_API_KEY=$(cat "$_herdr_key_file")
  ANTHROPIC_AUTH_TOKEN=$LITELLM_API_KEY
  export LITELLM_API_KEY ANTHROPIC_AUTH_TOKEN
fi
unset _herdr_key_file

# Debian's /etc/profile resets PATH for login shells; put the runtime's own commands (codex wrapper, phone-holder,
# task-ask) back in front, as for the processes the supervisor starts.
case ":$PATH:" in *:/opt/herdr-runtime/bin:*) ;; *) PATH=/opt/herdr-runtime/bin:$PATH; export PATH ;; esac
