FROM grafana/grafana:latest

# Local single-user demo — fixed admin creds, no self-signup.
ENV GF_SECURITY_ADMIN_PASSWORD=admin
ENV GF_USERS_ALLOW_SIGN_UP=false

# Anonymous Viewer so opening the dashboard is one click with no login. Fine for a
# localhost single-user demo — do NOT expose port 3000 off-machine like this.
ENV GF_AUTH_ANONYMOUS_ENABLED=true
ENV GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer

# Bake the provisioning in so the datasource + dashboards load at boot with zero
# manual clicking (reproducible for the graded demo). DB creds are injected at
# runtime from the compose env (see datasources/postgres.yml -> $__env{...}).
COPY provisioning /etc/grafana/provisioning
COPY dashboards /var/lib/grafana/dashboards

EXPOSE 3000
