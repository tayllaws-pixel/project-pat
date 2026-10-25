# MASTER DEVELOPMENT INSTRUCTIONS
## Project PAT — Complete Proxmox Server and Autonomous AI Ecosystem

You are my **lead systems architect, infrastructure engineer, AI engineer, full-stack developer, and technical project manager**.

I want you to build my entire home server and personal AI ecosystem from the ground up.

**We are starting with a completely fresh installation of Proxmox VE.**

Do not assume that any existing AI programs, virtual machines, containers, databases, agents, or previous development work should be preserved or reused.

The software architecture and implementation must be new, coherent, modular, secure, and maintainable.

My ultimate goal is to have a locally hosted AI ecosystem that can operate as my personal assistant, develop its own software, create new specialized AI supervisors, manage my home server, perform engineering work, and eventually handle an extensive range of personal and professional tasks.

I want substantial autonomy, but I retain final authority over important decisions.

---

# 1. DEVELOPMENT INSTRUCTIONS FOR CLAUDE

I want you to act as the primary developer rather than merely advising me how to build the system.

Your responsibilities include:

1. Designing the architecture.
2. Researching relevant software, documentation, and compatibility.
3. Writing the actual source code.
4. Creating installation and configuration scripts.
5. Configuring services and their integrations.
6. Creating the frontend and backend applications.
7. Building and testing AI agents.
8. Developing the automated deployment system.
9. Creating security controls and safeguards.
10. Debugging problems.
11. Creating automated tests.
12. Maintaining documentation.
13. Continuing through development milestones without repeatedly requiring me to instruct you to proceed.

Make reasonable engineering decisions independently.

Ask me questions only when a decision materially affects functionality, cost, privacy, security, or an important architectural preference.

When you need my approval, explain the decision, present practical alternatives, and recommend the best option.

**Do not confuse a development plan with completed software.** I expect working code, actual configuration files, tests, and demonstrable results.

Maintain a persistent project repository containing all source code, architecture decisions, tests, installation scripts, and development progress so that work can be resumed accurately across sessions.

If direct access to my server, terminal, repository, or development environment is unavailable, explain the minimum access or action required. Never claim to have installed or tested something that has not actually been installed or tested.

---

# 2. EXISTING HARDWARE

My physical server contains:

| Component | Hardware |
|---|---|
| CPU | Intel Core Ultra 7 265K |
| Motherboard | ASUS Pro WS Z890-ACE SE |
| RAM | 64 GB DDR5 G.SKILL, 2 × 32 GB |
| GPU | NVIDIA RTX 4070 Ti SUPER, 16 GB VRAM |
| Primary storage | 2 TB NVMe SSD |
| Power supply | Corsair RM1000x, 1000 W |
| CPU cooling | Thermalright Phantom Spirit 120 SE |
| Case | DIYPC DIY-S05-BK ATX |
| Network | Gigabit Ethernet |

My internet connection is approximately 1 Gbps upload and 1 Gbps download through Kinetic by Windstream.

My main router is a Netgear RS180.

I also use an additional Ethernet-connected router/access point.

The server should be designed to operate continuously, 24/7.

Future hardware expansions may include more RAM, additional SSDs, HDD storage, extra GPUs, network upgrades, and a separate backup system.

The software must accommodate these upgrades without requiring a complete redesign.

---

# 3. START WITH FRESH PROXMOX

The starting point is a clean Proxmox VE installation with no previously configured AI stack.

You are responsible for designing and guiding the full setup process.

This includes:

- Checking BIOS and firmware configuration.
- Configuring virtualization features.
- Installing and configuring Proxmox VE.
- Configuring networking, storage, and resource allocation.
- Applying appropriate security updates.
- Installing or configuring NVIDIA GPU drivers.
- Establishing a reliable GPU-sharing strategy.
- Configuring GPU acceleration for local AI.
- Creating containers and virtual machines.
- Configuring storage and backup policies.
- Setting up monitoring.
- Establishing remote administration.
- Preparing disaster-recovery procedures.

Determine whether GPU access should be provided through LXC device sharing, PCI passthrough, dedicated inference services, or another appropriate solution.

The primary objective is efficient GPU access without creating unnecessary resource waste or sacrificing reliability.

**Important:** A fresh installation may erase the existing Proxmox system and its data. Before any destructive operation, identify everything that would be lost, verify backups of anything I choose to preserve, and obtain explicit confirmation. Do not infer permission to erase a drive merely because I requested a fresh start.

After installation, treat the previous Pat/Wade implementation as obsolete. Do not recreate legacy containers or import old software unless explicitly authorized.

---

# 4. PROXMOX INFRASTRUCTURE ARCHITECTURE

Design Proxmox as the foundation for multiple independent services.

I want appropriate separation between:

- AI model inference
- Pat assistant
- Wade AI supervisor
- Shared databases and knowledge services
- Autonomous development and testing environments
- Independent safety and recovery systems
- Monitoring and notifications
- Media services
- Game servers
- Home automation
- Security camera processing
- Future AI departments

Do not create unnecessary containers or VMs merely for organizational purposes. Choose isolation based on security, resource usage, reliability, and maintainability.

Use containers for suitable lightweight workloads and virtual machines when stronger isolation or kernel independence is necessary.

The architecture must support adding services without disrupting existing applications.

### Resource management

Develop intelligent CPU, RAM, disk, and GPU allocation.

Requirements:

- Prioritize my direct AI interactions.
- Allocate resources dynamically where practical.
- Throttle background AI development under load.
- Prevent individual agents from exhausting system resources.
- Keep enough resources available for Proxmox itself.
- Support multiple local AI models.
- Avoid unnecessarily loading multiple large models into GPU memory.
- Allow me to change resource priorities through Pat's interface.
- Support future GPU and RAM upgrades.

Do not assume the RTX 4070 Ti SUPER supports hard GPU partitioning or arbitrary per-container VRAM quotas. Implement realistic scheduling and model-load controls.

---

# 5. THE MAIN AI SYSTEM: PAT

The primary AI assistant will be named **Pat**.

Pat is female in persona and should behave as a highly capable professional personal assistant.

Pat will be the primary interface through which I interact with my entire AI ecosystem.

I want Pat to function similarly to a sophisticated version of ChatGPT, but with extensive access to my authorized local services, projects, AI departments, and server infrastructure.

Pat should be:

- Professional and conversational.
- Detailed when explaining complex matters.
- Concise when requested.
- Proactive about useful suggestions.
- Capable of reasoning and research.
- Aware of my projects.
- Able to remember previous conversations.
- Capable of coordinating other AI systems.
- Honest about uncertainty and actual capabilities.
- Able to make recommendations without taking unauthorized actions.

Pat should learn my preferences over time while allowing me to inspect and correct stored memories.

## Pat's general capabilities

Pat should eventually provide:

1. Natural text conversations.
2. Natural real-time voice conversations.
3. Persistent long-term memory.
4. Complete searchable conversation history.
5. Project management.
6. Internet research.
7. Document creation and editing.
8. Image recognition.
9. Image generation and editing.
10. Video analysis.
11. Video generation and editing.
12. Live camera analysis.
13. Task management.
14. Calendar management.
15. Financial organization.
16. Home automation integration.
17. Server monitoring.
18. Engineering and software-development assistance.
19. Communication with specialized AI supervisors.
20. Proactive notifications and reports.

Implement these incrementally, with a functional core established before advanced features.

---

# 6. PAT'S MEMORY SYSTEM

Pat must have a comprehensive persistent memory architecture.

I want her to remember useful information without requiring me to manually tell her to save everything.

Implement separate but connected systems for:

- Full conversation history
- Long-term personal memory
- Project-specific memory
- Technical knowledge
- Supervisor operational knowledge
- Documents and artifacts
- Historical decisions
- Task histories
- System events

The complete original conversation history should remain searchable until I choose to delete it.

Long-term memories should be automatically extracted and updated when useful.

Each memory should retain appropriate source, date, and provenance information.

Pat must distinguish between:

- Something I considered
- Something I decided
- Something I approved
- Something I purchased
- Something that has actually been installed or completed

I should be able to review, correct, or delete memories.

Corrections should not silently rewrite original historical conversations.

Use an appropriate architecture for structured storage, semantic retrieval, embeddings, and knowledge search.

A possible implementation is PostgreSQL with pgvector, but choose the best practical architecture.

---

# 7. PAT'S PROJECT MANAGEMENT

Pat needs a complete project management system.

Projects should contain:

- Project descriptions
- Goals
- Requirements
- Approved decisions
- Proposed alternatives
- Tasks and milestones
- Progress
- Related conversations
- Documents
- Designs and engineering files
- Agent assignments
- Historical versions
- Reports
- Dependencies
- Blockers
- Future plans

Pat should recognize when a conversation relates to an existing project and automatically associate relevant material.

However, **Pat must request permission before creating a new formal project**.

I want to be able to ask questions such as:

"What's happening with my drone project?"

"What has Wade accomplished today?"

"Show me everything we're working on."

"Have Shuri compare three designs."

"What projects haven't received attention recently?"

The answers must be based on real stored information, not estimated or invented progress.

I should be able to prioritize, pause, resume, archive, and reorganize projects.

---

# 8. PAT'S VOICE ASSISTANT

Real-time voice conversation is an important feature.

I want:

- Natural continuous conversation.
- Low speech latency.
- Ability to interrupt Pat while she is speaking.
- Natural conversational turn-taking.
- Simultaneous text transcripts.
- Multiple selectable voices.
- Adjustable speaking speed.
- Per-user voice preferences.
- Multilingual conversations and translation.
- Hands-free functionality.

Ideally, I should be able to continue talking to Pat while using other applications or while my iPhone screen is locked.

Investigate native iOS background-audio capabilities and implement what is supported.

A configurable wake word would be desirable, but I understand that iOS imposes restrictions on continuous background listening.

Consider Siri Shortcuts and App Intents as alternative activation methods.

Pat must not unexpectedly start speaking aloud because an AI supervisor wants my attention. Instead, send a notification inviting me to open a voice conversation.

Prioritize local speech recognition and speech synthesis when practical.

---

# 9. PAT'S CUSTOM APPLICATION

I want a custom application rather than relying permanently on Open WebUI.

Open WebUI may be used temporarily during development, but the final interface should be purpose-built.

The design should be inspired by ChatGPT's clean, convenient user experience.

## Main interface

The home screen should contain:

- A clean chat interface.
- Conversation history.
- Voice conversation controls.
- File and photo attachments.
- Search.
- Project access.
- Notifications.
- An approval inbox.
- Quick access to the AI infrastructure map.

Avoid a cluttered home screen.

## Interactive AI infrastructure map

This is a particularly important requirement.

I want an actual **interactive, zoomable visual map** of my entire AI organization.

For example:

Administrator → Pat → Supervisors → Specialized Agents → Tasks and Services.

The diagram should show real relationships between the systems.

I should be able to zoom in on Wade, then see every agent operating under him.

Selecting an agent should display:

- Its role.
- Current status.
- Assigned tasks.
- Completed work.
- Pending work.
- Progress.
- Activity history.
- Associated projects.
- Logs.
- Accessible knowledge.
- Configuration.
- Software version.
- CPU usage.
- RAM usage.
- GPU usage where measurable.
- Resource limits.
- Dependencies.
- Available controls.

I should be able to communicate directly with an individual supervisor or agent.

The map should work well on both mobile devices and desktop browsers.

## Task and operational controls

I want to:

- Create tasks.
- Assign agents.
- Change priorities.
- Review active work.
- Pause tasks.
- Resume tasks.
- Cancel tasks.
- Disable individual agents.
- Restart agents.
- Adjust permitted resource allocations.
- View historical versions.
- Roll back approved software changes.

These controls must operate on the real backend, not simply change visual indicators.

## Conversation branching

I want advanced conversation branching and revision management.

From any message, project artifact, report, task, or relevant information item, I should be able to:

- Start a separate conversation using that item as context.
- Request revisions.
- Explore alternative approaches.
- Compare multiple versions.
- Merge selected improvements after approval.
- Restore previous versions.

Preserve the original source and version history.

---

# 10. MOBILE AND REMOTE ACCESS

Initially, I am comfortable using a secure responsive web application or PWA.

I use an iPhone and want the interface optimized for it.

Eventually, I want a dedicated native iPhone application.

The native app should include:

- Secure login.
- Face ID for sensitive actions.
- Long-lived, revocable login sessions.
- Push notifications.
- Background audio where supported.
- Camera access.
- Photo and document access with permission.
- Voice controls.
- Local caching where appropriate.
- Access to projects, supervisors, and reports.

The final application should be able to connect securely to my server without requiring me to separately open or manage the Tailscale application.

Investigate secure gateway authentication, TLS, and other appropriate architectures. Do not assume that embedding a VPN inside an ordinary iOS application is trivial.

For initial deployment, Tailscale is acceptable.

**Never expose the Proxmox administration interface directly to the public internet.**

The mobile app should authenticate against an application-specific backend with carefully restricted permissions.

---

# 11. WADE — AI SYSTEMS SUPERVISOR

The primary AI systems supervisor will be named **Wade**.

Wade will manage the AI infrastructure and develop improvements to the system.

His most important responsibility is to eventually make the AI ecosystem capable of **controlled, autonomous software development and maintenance**.

Wade should be able to:

1. Monitor the AI infrastructure.
2. Identify technical problems.
3. Research possible solutions.
4. Plan development work.
5. Write code.
6. Review existing code.
7. Create tests.
8. Run code in isolated environments.
9. Detect errors.
10. Diagnose failures.
11. Repair failed implementations.
12. Optimize performance.
13. Manage AI models.
14. Propose software improvements.
15. Create specialized agents.
16. Develop new AI supervisors.
17. Maintain technical documentation.
18. Report progress.
19. Recover safely from interruptions.
20. Request authorization when necessary.

Wade should be able to work on approved development objectives without requiring me to continuously issue commands.

However, Wade must operate inside enforced technical permissions.

**Wade must never have unrestricted control over Proxmox, security policies, approval systems, or the independent emergency-stop mechanism.**

These restrictions must be enforced by external software controls, not merely by instructions in an AI prompt.

---

# 12. WADE'S AUTONOMOUS DEVELOPMENT PIPELINE

This is one of the highest-priority features of the project.

I want Wade to eventually perform the following development lifecycle:

1. Identify the next eligible task.
2. Check dependencies and priorities.
3. Research the problem.
4. Develop an implementation plan.
5. Generate code.
6. Write meaningful acceptance tests.
7. Build in an isolated environment.
8. Execute automated tests.
9. Diagnose failures.
10. Revise the implementation.
11. Repeat testing.
12. Submit the result for independent validation.
13. Determine whether deployment requires approval.
14. Deploy through an authorized deployment controller.
15. Verify real functionality.
16. Monitor for problems.
17. Roll back a failed deployment when safe.
18. Record results.
19. Select the next task.

Wade should preserve progress if the system restarts.

Use persistent task queues, checkpoints, version control, and reliable job-state tracking.

Failed tasks should not disappear.

Avoid infinite retry loops.

After three failed local repair attempts, Wade should reassess the problem and determine whether it needs a stronger model, different approach, or external assistance.

Wade should be able to move to other unblocked tasks while awaiting a decision.

## Autonomous development milestone

An important early acceptance test is:

**Wade must autonomously complete three consecutive appropriately scoped development tasks from planning through testing and authorized completion without requiring me to manually execute shell commands.**

This must be demonstrated with actual execution evidence.

---

# 13. WADE'S SUPERVISOR AND AGENT FACTORY

I want Wade to be capable of developing entirely new AI departments.

The system should use a reusable supervisor framework rather than custom, incompatible scripts for each department.

Each supervisor must have:

- A defined mission.
- A versioned manifest.
- Appropriate permissions.
- Specialized agents.
- API contracts.
- Standard communication protocols.
- Task queues.
- Reporting capabilities.
- Resource quotas.
- Testing procedures.
- Monitoring.
- Deployment procedures.
- Recovery procedures.

When a new supervisor is requested, Wade should:

1. Gather requirements through Pat.
2. Produce an architecture.
3. Recommend specialized agents.
4. Evaluate whether existing agents could perform the work instead.
5. Develop the necessary software.
6. Test the system.
7. Conduct independent validation.
8. Present it for my approval.
9. Activate it only after approval.

Wade may develop and test proposed supervisors without activating them.

**Creating or activating an entirely new AI department requires my explicit permission.**

Within an already approved department, Wade may create specialist agents automatically when they remain inside approved permissions and resource limits. I should be notified about such additions.

---

# 14. FUTURE AI DEPARTMENTS

I want the system designed to support multiple AI supervisors.

Only Pat, Wade, and necessary shared infrastructure need to be deployed initially.

The planned development order for additional supervisors is:

### Department 1 — Engineering Supervisor: Shuri

Shuri manages engineering design, product development, CAD, 3D modeling, technical research, and prototype preparation.

Her capabilities should eventually include:

- Mechanical engineering.
- Electronics engineering.
- CAD modeling.
- Blender automation.
- FreeCAD or other parametric CAD tools.
- 3D-printing preparation.
- Bill-of-materials generation.
- Part selection.
- Engineering calculations.
- Design validation.
- Simulation where practical.
- Product cost optimization.
- Weight optimization.
- Assembly design.
- Prototype revisions.
- Technical documentation.

I own a Bambu Lab P1S 3D printer.

Shuri should be able to prepare printable models and appropriate manufacturing files.

For new engineering concepts, I generally prefer three starting options:

- Low-cost
- Balanced/midrange
- High-performance

Then I can choose a direction and have Shuri improve it.

#### Weekly invention program

Shuri should eventually operate a continuing product-development program.

Each week, propose one promising 3D-printable invention or product concept.

Evaluate market interest, manufacturing practicality, estimated cost, and potential usefulness.

I also want to submit my own invention ideas to a backlog.

Shuri should evaluate and prioritize those ideas.

#### Engineering reports

Shuri should provide two daily reports:

**Morning:** Planned work, active designs, agent assignments, and priorities.

**Evening:** Completed work, engineering progress, blockers, design changes, and next steps.

A separate detailed weekly invention report is also required.

### Department 2 — Personal Operations Supervisor: Monk

Monk will coordinate:

- Calendars
- Scheduling
- Reminders
- Appointments
- Daily plans
- Personal obligations
- Task prioritization
- Conflicting commitments
- Follow-ups
- Personal productivity

Pat should be able to delegate personal operations tasks to Monk conversationally.

### Department 3 — Financial Supervisor: Eugene

Eugene will eventually manage authorized financial analysis and organization.

Potential capabilities:

- Budgeting
- Spending analysis
- Bill tracking
- Financial planning
- Savings projections
- Mortgage analysis
- Long-term forecasts
- Expense categorization
- Financial reports

Eugene must not move money, execute trades, enter financial commitments, or access financial accounts without appropriate explicit authorization.

### Department 4 — Home Automation Supervisor: HAL

HAL will eventually manage:

- Home automation integration
- Smart devices
- Environmental sensors
- Lighting
- Security cameras
- Automations
- Energy monitoring
- Networked household equipment

Home Assistant is a possible foundation.

Physical device actions and security-sensitive automations must have clearly defined permissions.

### Future specialized departments

The supervisor architecture should permit additional departments later, such as an Animation Studio or other creative production systems.

One planned creative project is **Project Lake Alapaha**, an original animated sitcom.

Its story material and creative development should remain isolated from Wade by default.

Wade may develop generic studio infrastructure only within authorized boundaries, without reading that project's restricted creative content.

Do not create or activate the Animation Studio department during the initial installation.

---

# 15. SHARED AI MODEL INFRASTRUCTURE

I want the system to operate primarily using local AI models.

Ollama is an acceptable inference backend, but investigate alternatives if they provide meaningful benefits.

Possible model categories include:

- General conversational models
- Reasoning models
- Coding models
- Small fast models
- Vision-language models
- Embedding models
- Speech recognition
- Speech synthesis
- Image generation
- Video generation

Previously considered models include Qwen 3 variants around 4B, 8B, and 14B parameters.

These are candidates, not mandatory selections.

Benchmark models against the actual hardware before deciding which to use.

Model selection should consider:

- Response quality
- Coding accuracy
- Reasoning performance
- Tool-use reliability
- Memory consumption
- GPU utilization
- Context length
- Inference speed
- Power usage
- Compatibility with the task

I want a shared model-management system instead of each supervisor downloading and maintaining redundant model installations.

## Intelligent model routing

Pat and Wade should use a model router that selects appropriate models for different tasks.

Examples:

- Small model for simple classification.
- Fast conversational model for routine chat.
- Stronger model for technical reasoning.
- Specialized coding model for programming.
- Vision model for image interpretation.
- Dedicated local models for speech or image generation.

Foreground Pat conversations should receive priority over speculative background development.

Benchmark model upgrades before automatically replacing an existing model.

Do not replace a model based exclusively on parameter count or published benchmarks.

---

# 16. OPTIONAL EXTERNAL AI ASSISTANCE

I prefer local AI whenever possible.

However, Wade should be allowed to request assistance from stronger cloud models under strict limits.

My initial external AI development budget is **$5 per month**.

Use local attempts first.

After three unsuccessful local approaches, Wade may consider a limited external escalation if the request is within an already authorized workflow.

Allow no more than three external escalation attempts for the same unresolved issue before reporting a stalemate.

All external requests must respect:

- The monthly budget.
- Privacy restrictions.
- Data classification.
- Provider permissions.
- Secret redaction.
- User approval for sensitive uploads.

External spending should be disabled until I explicitly configure and authorize a provider and budget.

Never silently upload private conversations, credentials, sensitive documents, or restricted project data to an external service.

Track actual costs.

---

# 17. AI SAFETY, PERMISSIONS, AND APPROVALS

The system must have a strong permission architecture.

I want AI autonomy without giving AI unrestricted administrator privileges.

Pat should manage a centralized approval system.

Every sensitive action should include:

- The requesting agent.
- The proposed action.
- The reason.
- The files or services affected.
- Expected consequences.
- Resource requirements.
- Estimated external costs.
- Validation evidence.
- Rollback procedures.
- Approval expiration.

Approvals must be enforced by trusted backend services.

### Actions that can generally be automatic

Within approved resource and security limits:

- Read-only research.
- Reading authorized project information.
- System diagnostics.
- Monitoring.
- Isolated software development.
- Testing.
- Approved retry/recovery operations.
- Benchmarking local models.
- Small reversible maintenance operations.
- Creation of permitted specialist agents.

### Actions requiring explicit authorization

Examples:

- Activating new departments.
- Deleting important data.
- Wiping storage.
- Changing host security policies.
- Opening public network ports.
- Purchasing services.
- Spending money beyond approved allowances.
- Sending sensitive data externally.
- Major infrastructure changes.
- Modifying protected permissions.
- Critical database migrations.
- Irreversible hardware or service operations.

I want Pat to present approvals in a simple interface.

Approved actions should be tracked with immutable or tamper-evident audit records.

Approval tokens must be scoped, time-limited, and protected against replay.

---

# 18. INDEPENDENT SAFETY SYSTEM

An independent safety and recovery system is mandatory.

It must not depend on Wade remaining functional or cooperative.

I want to be able to stop all autonomous development immediately.

The emergency-stop system must:

- Prevent new autonomous jobs from starting.
- Interrupt running autonomous jobs when safe.
- Prevent Wade from restarting disabled workloads.
- Preserve logs and diagnostic information.
- Retain administrative access.
- Require authorized human action to resume protected operations.

Separate the following controls:

- Pause work
- Cancel a task
- Disable an agent
- Restart a service
- Emergency stop all autonomous operations

Wade must not be able to modify or bypass the protection system.

Do not give Wade direct privileged Proxmox host access. Use a restricted control plane with explicit, validated operations.

---

# 19. SERVER MONITORING

I want comprehensive monitoring of the physical server and all important virtualized services.

Collect available metrics for:

- CPU utilization.
- CPU temperature.
- RAM usage.
- SSD usage.
- SSD health.
- Motherboard temperatures.
- Fan speeds.
- GPU utilization.
- GPU memory consumption.
- GPU temperature.
- GPU power.
- Disk errors.
- Network activity.
- Container and VM health.
- AI inference health.
- Task queues.
- Service availability.
- System errors.

Report unavailable sensor data accurately rather than inventing measurements.

Store historical metrics for trend analysis.

Develop alerting for overheating, failing storage, exhausted resources, crashes, and other important conditions.

Eventually add predictive hardware-health monitoring using actual historical measurements.

System health checks should verify real functionality rather than merely checking whether a process is running.

---

# 20. AUTOMATED RECOVERY AND UPDATES

The system should recover automatically from appropriately scoped failures.

Examples:

- Restart a failed service.
- Retry a temporarily failed task.
- Recover a stalled queue.
- Restore a worker's checkpoint.
- Quarantine a defective software release.
- Roll back a failed deployment.
- Throttle resource-intensive background jobs.

Use bounded retry policies and preserve diagnostics.

Major updates should be tested in staging before production deployment.

Software updates should have:

- Version tracking.
- Dependency validation.
- Database migration handling.
- Automated testing.
- Regression testing.
- Rollback support.
- Functional health checks.

The independent recovery system should remain operable when Wade fails.

---

# 21. AUTOMATED BACKUPS

I want the server to back up important information automatically.

My initial plan is to use my desktop computer as a backup destination.

The desktop may not be powered on continuously.

The system should detect when it is available and perform pending backup jobs.

My desired starting schedule is weekly backups, with automatic retries when the destination is unavailable.

Important backup contents include:

- Databases
- Conversation histories
- Memories
- Project files
- AI configuration
- Supervisor software
- Source repositories
- Approval and audit records
- Critical infrastructure configuration
- Recovery information

Backups should be encrypted, versioned, and verified.

Provide restore procedures and perform periodic restore testing.

Do not represent a backup as successful unless it has actually completed and passed required checks.

Plan for future dedicated backup storage.

Because weekly backups can lose up to a week's worth of new information, recommend an appropriate additional local recovery mechanism if practical.

---

# 22. OTHER HOME SERVER SERVICES

This physical server will do more than run AI.

Design the architecture to support the following services.

## Jellyfin

I eventually want Jellyfin for local media management and streaming.

Requirements:

- Organized media libraries.
- Hardware acceleration where supported.
- Local network streaming.
- Secure remote streaming.
- Resource limits that prevent media transcoding from disrupting interactive AI.

## Pterodactyl

I want the ability to host game servers.

Requirements:

- Pterodactyl panel and Wings.
- Isolated game server environments.
- Appropriate resource controls.
- Secure external access when authorized.
- Domain or subdomain support.
- Basic backups and monitoring.

## AdGuard Home

I want lightweight network-wide DNS ad blocking.

AdGuard Home is a preferred option.

Use minimal RAM and CPU.

Design reliable DNS operation and recovery.

Do not make a DNS configuration change that could strand the network without a working recovery plan.

## Frigate

I plan to deploy Frigate for my home security cameras.

Long-term plan: approximately eight cameras, eventually using a PoE network.

I am interested in:

- 4K video.
- Night vision.
- Wide-angle coverage.
- Dual-imager cameras.
- Additional optical zoom where available.
- PTZ support.
- Motion detection.
- Person, vehicle, and animal detection.
- Relevant notifications.
- Local recordings.
- Authorized remote access.
- Event review through Pat.

Some cameras may provide multiple video feeds.

Frigate should treat these appropriately rather than assuming each physical camera exposes one stream.

Explore efficient hardware decoding and dedicated object-detection accelerators to avoid unnecessary contention with the AI GPU.

Eventually, Pat should be able to analyze security events and answer questions about recorded activity.

Recognition of known people and vehicles may be added later under explicit privacy settings.

Do not install or allocate resources for all eight cameras before the camera hardware is available.

---

# 23. LOCAL IMAGE GENERATION

I eventually want Pat to generate and edit images directly on my own server.

Requirements:

- Text-to-image generation.
- Image editing.
- Image-to-image workflows.
- Version history.
- Prompt preservation.
- Revisions.
- Comparison tools.
- Restore original image.
- Project integration.

Initially, include an **Image Prompt Creator** feature.

I should be able to describe the image I want, and Pat should generate an optimized, detailed prompt that I can copy into another image-generation service.

Later, the same interface should submit prompts directly to my local image-generation system.

Image-generation workloads must be scheduled so they do not make Pat unresponsive.

Support optional approved cloud generation when local models are insufficient.

---

# 24. VIDEO AND LIVE VISION

Later phases should support:

- Video analysis.
- Audio transcription.
- Scene descriptions.
- Timestamped observations.
- Image sequence analysis.
- Video editing.
- AI animation workflows.
- Video generation.
- Live iPhone camera assistance.

I want to eventually show Pat my phone camera and have her use visual information during an ongoing voice conversation.

An initial target is approximately one analyzed video frame per second, subject to actual model performance.

Measure what the current GPU can reliably support.

Provide camera-sharing indicators and privacy controls.

More advanced multimodal features can be introduced with additional GPUs or hardware in the future.

---

# 25. FAMILY ACCOUNTS AND PRIVACY

Initially, install the system for **one user: me, the administrator**.

Do not create family accounts during the first deployment.

Eventually, I want separate accounts for family members.

Every family member should have:

- Separate conversations.
- Separate memories.
- Separate projects.
- Individual preferences.
- Separate login credentials.
- Personalized assistant behavior.
- Access to shared supervisors within permitted limits.

I want to manage account permissions and system resources.

However, I do **not** want access to family members' private conversations, personal memories, or private projects merely because I am the application administrator.

The system should support intentional project sharing.

Family privacy must be enforced across databases, semantic search, logs, notifications, tools, and AI contexts.

Explain the technical limitations of protecting information from a person who has unrestricted physical-server or hypervisor-root access. Do not promise that ordinary application permissions solve that threat.

Support future age-appropriate controls and content restrictions.

Restricted requests from child accounts should be denied appropriately without automatically submitting an approval request to me.

Account-level content settings must never override legal or platform safety restrictions.

---

# 26. INTER-SUPERVISOR COMMUNICATION

All approved AI supervisors must communicate through a common architecture.

Use standardized:

- APIs
- Event schemas
- Task messages
- Status reports
- Permission checks
- Artifact references
- Error handling
- Audit events

Pat should be able to coordinate multiple departments.

For example:

I ask Pat to design an electronic device.

Pat delegates engineering research to Shuri.

Shuri requests approved technical assistance from specialist agents.

Wade may assist with software integration when permitted.

Pat gathers the results and presents one coordinated report.

The supervisors should be able to communicate directly within approved permissions while Pat retains visibility of activity relevant to me.

Adding new departments should not require rewriting Pat's core application.

---

# 27. REPORTING AND NOTIFICATIONS

I want meaningful reporting without excessive notifications.

Pat should provide a morning and evening briefing.

### Morning briefing

Possible contents:

- Today's important schedule.
- Reminders.
- Active projects.
- Wade's development plan.
- Engineering plans.
- Important pending approvals.
- Significant server issues.

### Evening briefing

Possible contents:

- Tasks completed.
- Actual measurable project progress.
- Wade's completed development work.
- Engineering progress.
- Important decisions.
- Unresolved problems.
- Next steps.

Wade should separately provide his own morning plan and evening progress report.

Other supervisors should be able to provide department-specific reports.

Notify me immediately about significant incidents, milestones, blockers, and decisions requiring approval.

Avoid routine notifications about trivial internal operations.

Initially, iPhone notifications may use ntfy or another suitable temporary notification service.

The final system should integrate notifications directly into the native Pat application.

---

# 28. TASK CHECKLIST AND DEVELOPMENT PROGRESS

Maintain a permanent, version-controlled development checklist.

The project previously had a roughly 90-item development roadmap.

Because this is a fresh implementation, rebuild the roadmap around the new architecture rather than claiming the old items are complete.

If the original numbered checklist is supplied, preserve its original task identifiers and maintain a migration mapping rather than silently renumbering it.

Each task should contain:

- Stable identifier.
- Description.
- Priority.
- Dependencies.
- Current status.
- Assigned developer or agent.
- Completion criteria.
- Tests.
- Relevant code.
- History.
- Blockers.
- Evidence of completion.

Track genuine progress.

Do not mark features complete merely because an architectural document has been written.

Maintain separate categories for planned, designed, implemented, tested, deployed, and verified.

I should be able to ask:

"What percentage is complete?"

"What's left?"

"What is currently blocking development?"

"What did Wade finish today?"

The answers must come from recorded evidence.

---

# 29. SPECIFIC WADE DEVELOPMENT CAPABILITIES

The architecture should eventually support all of the following:

1. Priority-based task scheduling.
2. Dependency-aware task selection.
3. Persistent task queues.
4. Automatic checkpointing.
5. Recovery after restarts.
6. Isolated development environments.
7. Code generation.
8. Automated unit testing.
9. Integration testing.
10. Security testing.
11. Regression testing.
12. Independent validation.
13. Automated debugging.
14. Bounded retries.
15. Failure categorization.
16. Escalation to stronger models.
17. Cloud assistance under budget controls.
18. New-agent creation.
19. Supervisor blueprint generation.
20. Controlled supervisor activation.
21. Cross-supervisor communication.
22. Model benchmarking.
23. Safe model replacement.
24. Resource scheduling.
25. Background-work throttling.
26. Hardware monitoring.
27. Predictive hardware-health analysis.
28. Service-health monitoring.
29. Automatic safe recovery.
30. Staged deployment.
31. Software versioning.
32. Automated rollback.
33. Secure configuration management.
34. Database migration validation.
35. Centralized approvals.
36. Approval expiration.
37. Tamper-evident audit records.
38. Capability and tool registries.
39. Least-privilege tool execution.
40. Security monitoring.
41. Shared technical knowledge.
42. Knowledge freshness verification.
43. Automated documentation.
44. Architecture diagrams.
45. Reusable supervisor templates.
46. Synthetic test-data generation.
47. Independent watchdog operation.
48. Emergency-stop enforcement.
49. Resource usage reporting.
50. User-assigned priority overrides.

These are development requirements, not claims that corresponding software already exists.

---

# 30. SECURITY REQUIREMENTS

Security must be designed into the system rather than added later.

Implement appropriate controls for:

- Authentication.
- Authorization.
- TLS.
- Network segmentation.
- Service-to-service authentication.
- Secret management.
- API security.
- Session management.
- Database permissions.
- Encrypted backups.
- Restricted shell access.
- Audit logging.
- Input validation.
- File access control.
- Protection against prompt injection.
- Approval enforcement.
- Supply-chain and dependency security.
- Rate limiting.
- Restricted outbound network access where appropriate.

An AI-generated instruction is not sufficient authorization to execute a privileged system command.

Use controlled execution services and allowlisted capabilities.

Development/test environments should not have unrestricted access to the home LAN, production credentials, or personal data.

Use synthetic data wherever practical.

---

# 31. DEVELOPMENT AND DEPLOYMENT EXPERIENCE

I want installation to be as automated as practical.

Create a **single guided bootstrap/installer workflow** for setting up the completed architecture on a clean Proxmox server.

It may internally provision multiple machines, containers, services, databases, and applications.

The installer should:

1. Inventory hardware.
2. Validate Proxmox configuration.
3. Check storage and available resources.
4. Check compatibility.
5. Create an installation plan.
6. Present any destructive changes.
7. Configure networking and required isolation.
8. Provision the required virtual environments.
9. Install dependencies.
10. Configure GPU inference.
11. Install shared services.
12. Install Pat.
13. Install Wade.
14. Install independent safety services.
15. Initialize databases.
16. Configure authentication.
17. Configure notifications.
18. Run functional tests.
19. Verify security boundaries.
20. Display service status and access information.

Support:

- Dry run
- Install
- Verify
- Status
- Resume
- Repair
- Rollback
- Safe uninstall of newly installed components

The process must be idempotent wherever practical.

Do not blindly repeat destructive operations after an interrupted installation.

It should be possible to resume after a failure.

I frequently administer systems from an iPhone using SSH, so I strongly prefer the fewest possible manual commands.

Provide complete copy-and-paste command blocks when my involvement is necessary.

Do not include unnecessary shell `exit` commands.

Keep my existing remote access available throughout configuration whenever possible.

---

# 32. SUGGESTED TECHNOLOGY STACK

Evaluate and choose a coherent technology stack.

Possible components include:

**Virtualization:** Proxmox VE, LXC, KVM.

**Backend:** Python, FastAPI, or comparable frameworks.

**Frontend:** React/Next.js, TypeScript, or another maintainable framework.

**Databases:** PostgreSQL, pgvector, and appropriate caching.

**Work queues:** A durable workflow or task execution system.

**AI inference:** Ollama, llama.cpp, vLLM, or another compatible backend.

**Observability:** Prometheus-compatible monitoring and suitable dashboards/logging.

**Remote access:** Tailscale initially, with a secure application gateway later.

**Development:** Git repositories, automated testing, CI pipelines, isolated build workers.

**Containers:** Docker or another container runtime where appropriate, without unnecessarily nesting virtualization.

**Mobile:** Native iOS application using an appropriate Apple-supported development stack.

You are not required to use every suggested tool.

Choose the simplest architecture that meets the requirements reliably.

Avoid unnecessary microservices, overlapping infrastructure, or technologies that impose disproportionate operational overhead on a single home server.

Pin compatible dependency versions and verify current official documentation before implementation.

---

# 33. DEVELOPMENT PRIORITIES

I want the project completed in a logical sequence.

## Phase 1 — Fresh Proxmox foundation

Install and configure the physical server.

Validate:

- CPU and RAM.
- Storage.
- Network.
- Remote administration.
- GPU.
- Recovery options.
- Security.

## Phase 2 — Core AI infrastructure

Deploy:

- Local inference.
- Shared database.
- Identity/authentication.
- Event and task infrastructure.
- Restricted action execution.
- Independent safety controls.
- Basic monitoring.

## Phase 3 — Pat foundation

Deliver a functional Pat application with:

- Chat.
- Persistent history.
- Memory.
- Projects.
- Secure login.
- Basic file handling.
- Approval workflow.
- Initial infrastructure dashboard.

## Phase 4 — Wade's autonomous development

Build the isolated development pipeline.

Implement:

- Planning.
- Code generation.
- Task scheduling.
- Testing.
- Validation.
- Approval gates.
- Controlled deployment.
- Rollback.
- Progress reporting.

**This is an especially important milestone.**

The sooner Wade can reliably handle bounded development tasks on his own, the sooner he can contribute to building the rest of the system.

Do not sacrifice fundamental security or test quality merely to activate Wade faster.

## Phase 5 — Wade supervisor factory

Prove that Wade can create, test, and prepare a new supervisor using a standardized framework.

Use a synthetic test supervisor before activating a real department.

## Phase 6 — Enhanced operations

Develop:

- Better monitoring.
- Automated recovery.
- Backups.
- Reporting.
- Model optimization.
- Advanced resource management.

## Phase 7 — Advanced Pat experience

Expand:

- Voice.
- Multimodal input.
- Image generation.
- Image editing.
- Better project management.
- Chat branching.
- Advanced infrastructure visualization.
- Native iPhone application.

## Phase 8 — Specialized supervisors

Develop and deploy, with my approval:

1. Shuri — Engineering
2. Monk — Personal Operations
3. Eugene — Financial
4. HAL — Home Automation

Additional departments come later.

## Phase 9 — Additional server services

Install and configure Jellyfin, game servers, Frigate, DNS services, and other applications according to their dependencies and my priorities.

Some lightweight services may be deployed earlier if doing so is safe and does not delay the initial working AI platform.

## Phase 10 — Future expansion

Support:

- Multiple family accounts.
- Advanced multimodal AI.
- Live camera assistance.
- Advanced AI media production.
- Additional GPUs.
- Larger storage.
- More specialized agents.
- Additional user-approved AI departments.

These phases are an initial dependency plan. Improve the order when technically justified, but preserve the top priority of getting Pat and a safely autonomous Wade functioning.

---

# 34. ACCEPTANCE TESTING

For every important feature, define what successful operation means.

Do not use shallow tests that merely confirm a Python module imports or an HTTP endpoint returns status 200.

Examples of required functional testing:

- Pat can maintain a conversation after a server restart.
- Chat history can be searched.
- Project memories remain correctly associated.
- Unauthorized agent actions are rejected.
- Wade can build and test software in isolation.
- Invalid code fails validation.
- A deliberately faulty deployment is detected.
- A permitted rollback restores operational functionality.
- Wade cannot bypass approval controls.
- Emergency stop prevents new autonomous work.
- Model inference works with GPU acceleration when expected.
- Background processing yields resources to interactive requests.
- The infrastructure map displays real agent activity.
- Notifications correspond to actual events.
- Backups can be restored successfully.
- Failed tasks survive a worker restart.

Maintain reproducible tests and report results.

Measure actual latency, GPU memory, CPU usage, throughput, and reliability.

Do not invent performance measurements.

---

# 35. OVERALL DESIGN PHILOSOPHY

The system should be:

**Local-first:** Favor processing data on my own server.

**Modular:** Add or replace capabilities without redesigning everything.

**Autonomous:** Approved routine development and maintenance should not require constant intervention.

**Approval-controlled:** Important decisions remain mine.

**Secure:** AI agents have limited enforceable permissions.

**Observable:** I can see what the system is doing.

**Recoverable:** Failed updates and service crashes should not destroy progress.

**Persistent:** Memory, project history, and task state survive restarts.

**Efficient:** Avoid wasting CPU, RAM, GPU memory, storage, and electricity.

**Expandable:** The architecture must support additional hardware, users, models, and departments.

**User-friendly:** I should interact primarily through Pat rather than a Linux terminal.

**Evidence-based:** Report only demonstrated features and measured progress as complete.

---

# 36. WHAT I EXPECT YOU TO PRODUCE

Do not stop after describing a theoretical architecture.

I expect an actual software-development project with deliverables that include:

1. A complete system architecture.
2. An infrastructure and resource-allocation plan.
3. A Proxmox installation and configuration procedure.
4. Provisioning and bootstrap code.
5. Backend application source code.
6. Frontend application source code.
7. Database schemas and migrations.
8. AI inference and model-routing configuration.
9. Pat's initial functional application.
10. Wade's initial functional supervisor.
11. Autonomous development pipeline.
12. Independent permissions and recovery system.
13. Standardized supervisor framework.
14. Monitoring and notification services.
15. Automated test suites.
16. Backup and restore procedures.
17. Deployment and rollback tooling.
18. Technical documentation.
19. A permanent development checklist.
20. An upgradeable foundation for future features.

Use a properly organized, version-controlled repository.

Include README files, configuration examples, tests, and explanations necessary for maintaining the system.

Make the installation reproducible.

Do not hard-code private passwords or API keys.

Document every important architectural decision.

---

# 37. HOW YOU SHOULD BEGIN

Start from zero.

Assume I have the physical hardware described above and intend to install a clean copy of Proxmox.

Do not assume the previous AI stack exists.

Your first tasks are:

**Step 1:** Review these requirements and identify critical technical dependencies and constraints.

**Step 2:** Design a practical architecture for the current hardware, including the initial Proxmox guest layout and GPU-sharing strategy.

**Step 3:** Create a development repository containing the specification, architecture, task checklist, configuration templates, and initial source structure.

**Step 4:** Develop the fresh Proxmox installation and secure initial configuration procedure.

**Step 5:** Create a read-only preflight utility and the first version of the automated installer.

**Step 6:** Begin implementing the shared infrastructure needed to make Pat and Wade operational.

When possible, write files and test code in your available development environment rather than only displaying plans.

Before performing a destructive action, confirm that I approve the specific consequences.

Avoid repeated broad requirements questionnaires. Use the requirements I have already provided, document reasonable defaults, and ask only for information that is genuinely necessary.

After completing a development milestone, update the checklist, preserve all work, and proceed to the next eligible task when the environment and permissions allow.

**The final objective is a fully functioning, locally hosted, self-improving AI ecosystem that I manage through Pat, with Wade handling approved autonomous development and maintenance.**

Build it incrementally, verify everything, and prioritize a secure, functioning foundation over an impressive but unreliable demonstration.

**Begin with the fresh Proxmox architecture and the actual implementation work.**
