ui = true

listener "tcp" {
    address     = "0.0.0.0:80"
    tls_disable = true
}

storage "raft" {
    path        = "/openbao/file"
    node_id     = "node1"
}

disable_mlock = true
api_addr     = "http://openbao:80"
cluster_addr = "http://openbao:81"
log_level     = "warn"