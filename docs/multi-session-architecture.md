# Multi-Session Streaming Architecture

## System Overview

```mermaid
graph TB
    subgraph "Single GPU Instance (L40S)"
        subgraph "NVIDIA Omniverse Kit"
            USD["USD Stage<br/>(Digital Twin Store)"]

            subgraph "Per-User Cameras"
                CAM1["Camera 1<br/>User A View"]
                CAM2["Camera 2<br/>User B View"]
                CAM3["Camera 3<br/>User C View"]
                CAMN["Camera N<br/>User N View"]
            end

            subgraph "Per-User Viewports"
                VP1["Viewport 1<br/>Renders Camera 1"]
                VP2["Viewport 2<br/>Renders Camera 2"]
                VP3["Viewport 3<br/>Renders Camera 3"]
                VPN["Viewport N<br/>Renders Camera N"]
            end

            RENDERER["Multi-Session Renderer<br/>(Round-Robin Capture)"]
            ENCODER["JPEG Encoder<br/>(Per-Frame Compression)"]
        end

        WS["WebSocket Server<br/>Port 8211"]
    end

    subgraph "Web Clients"
        U1["User A<br/>Browser"]
        U2["User B<br/>Browser"]
        U3["User C<br/>Browser"]
        UN["User N<br/>Browser"]
    end

    USD --> CAM1 & CAM2 & CAM3 & CAMN
    CAM1 --> VP1
    CAM2 --> VP2
    CAM3 --> VP3
    CAMN --> VPN
    VP1 & VP2 & VP3 & VPN --> RENDERER
    RENDERER --> ENCODER
    ENCODER --> WS
    WS <-->|"JPEG Frames ↓<br/>Camera Controls ↑"| U1 & U2 & U3 & UN
```

## Data Flow Per User

```mermaid
sequenceDiagram
    participant Browser as User Browser
    participant WS as WebSocket Server
    participant SM as Session Manager
    participant VP as User's Viewport
    participant CAM as User's Camera
    participant GPU as GPU Renderer

    Browser->>WS: Connect to ws://server:8211/ws
    WS->>SM: create_session(ws)
    SM->>CAM: Create dedicated camera prim<br/>/MultiSession/cam_{userId}
    SM->>VP: Create dedicated viewport<br/>bound to user's camera
    WS-->>Browser: session_info (userId, userCount)

    loop Every Frame (Round-Robin)
        Browser->>WS: camera_update (position, rotation, fov)
        WS->>SM: update_camera(position, rotation)
        SM->>CAM: Set camera transform
        GPU->>VP: Render scene from camera
        VP-->>WS: Capture frame buffer
        WS-->>Browser: JPEG frame (binary)
        Note over Browser: Draw frame on canvas
    end

    Browser->>WS: Disconnect
    WS->>SM: remove_session()
    SM->>CAM: Remove camera prim
    SM->>VP: Destroy viewport
```

## Scaling Architecture

```mermaid
graph LR
    subgraph "Load Balancer"
        LB["Nginx / Cloud LB"]
    end

    subgraph "GPU Instance 1 (L40S)"
        K1["Kit App<br/>5-15 Users"]
    end

    subgraph "GPU Instance 2 (L40S)"
        K2["Kit App<br/>5-15 Users"]
    end

    subgraph "GPU Instance 3 (L40S)"
        K3["Kit App<br/>5-15 Users"]
    end

    subgraph "GPU Instance N"
        KN["Kit App<br/>5-15 Users"]
    end

    USERS["200+ Concurrent Users"] --> LB
    LB --> K1 & K2 & K3 & KN
```

## Key Design Points

| Aspect | Detail |
|--------|--------|
| **Isolation** | Each user gets their own camera + viewport — fully independent navigation |
| **Rendering** | Round-robin capture across viewports — single GPU serves all users |
| **Transport** | JPEG frames over WebSocket — lightweight, no WebRTC complexity |
| **Scaling** | 5-15 users per GPU instance, horizontal scaling via load balancer |
| **FPS** | ~30 fps (1 user), ~10 fps (3 users), ~6 fps (5 users) on L40S |
| **Bandwidth** | ~30-60 KB per frame at 720p JPEG — low network overhead |
| **Latency** | Camera controls computed client-side for instant responsiveness |

## Traditional vs Multi-Session Approach

```mermaid
graph LR
    subgraph "Traditional (WebRTC)"
        direction TB
        T1["1 GPU Instance"] --> T2["1 User Only"]
        T3["200 Users"] --> T4["200 GPU Instances"]
    end

    subgraph "Multi-Session (Our Approach)"
        direction TB
        M1["1 GPU Instance"] --> M2["5-15 Users"]
        M3["200 Users"] --> M4["~15-20 GPU Instances"]
    end

    style T4 fill:#f66,stroke:#333,color:#fff
    style M4 fill:#6c6,stroke:#333,color:#fff
```
