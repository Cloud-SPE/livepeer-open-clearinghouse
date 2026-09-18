module github.com/livepeer/livepeer-open-clearinghouse-sdk-go/examples/streaming-http

go 1.23.0

require (
	github.com/decred/dcrd/dcrec/secp256k1/v4 v4.4.1
	github.com/livepeer/livepeer-open-clearinghouse-sdk-go v0.0.0
	golang.org/x/crypto v0.41.0
)

require (
	github.com/coder/websocket v1.8.14 // indirect
	golang.org/x/sys v0.35.0 // indirect
)

replace github.com/livepeer/livepeer-open-clearinghouse-sdk-go => ../../../sdks/go
