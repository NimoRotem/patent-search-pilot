In the following description, reference is made to the accompanying drawings, which form a part of this disclosure and show particular embodiments by way of illustration. Other embodiments may be used and structural or logical changes may be made without departing from the scope of the disclosure. Like reference numerals designate like parts throughout the several views.

Overview of the system

FIG. 1 shows a charging control system in which an edge controller 100 manages charging at a plurality of electric-vehicle connectors supplied by a common electrical branch. A branch conductor 102 supplies the plurality of connectors. The allowable aggregate current of the branch conductor 102, referred to in this description as the branch limit, is lower than the sum of the ratings of the connectors that the branch conductor 102 supplies. The branch limit is a stored value that corresponds to the aggregate current the branch is permitted to carry.

The branch conductor 102 supplies a first connector station 110 and a second connector station 112. Two connector stations are shown for clarity of illustration. The system accommodates a larger plurality of connector stations, and the description of the first connector station 110 applies to each further connector station of the plurality. The branch conductor 102 also supplies a non-charging load 114, which is load on the same branch other than the connector stations.

A branch current sensor 104 is located upstream of the contactors of the connector stations and measures the total current on the branch conductor 102. The branch current sensor 104 is coupled to the edge controller 100 and reports the measured branch current to it.

The edge controller 100 communicates with the connector stations over an isolated local bus 106. The isolated local bus 106 carries assigned current limits and contactor commands from the edge controller 100 to the control-pilot interfaces and the contactors of the connector stations, and carries control-pilot status and measured connector current from the connector stations to the edge controller 100. Each connector current sensor is coupled over the isolated local bus 106 to the edge controller 100 and reports the measured connector current to it. The isolated local bus 106 is isolated between the edge controller 100 and the connector stations.

A network interface 108 connects the edge controller 100 to a remote management system. As described below, the network interface 108 conveys remote schedules and authorizations to the edge controller 100 and conveys uploaded meter records from the edge controller 100, but the safety behaviour of the system does not depend on the availability of the network interface 108.

The connector station

FIG. 2 shows the first connector station 110 of FIG. 1 in greater detail. The first connector station 110 comprises a first contactor 120, a first connector current sensor 122, a first control-pilot interface 124, and a first electric-vehicle connector 126. The first contactor 120 is connected in the power path between the branch conductor 102 and the first electric-vehicle connector 126 and, when closed, permits current to flow from the branch conductor 102 to a vehicle coupled to the first electric-vehicle connector 126. The first connector current sensor 122 measures the connector current flowing in that power path, that is, the current actually drawn by the vehicle coupled to the first electric-vehicle connector 126.

The first control-pilot interface 124 presents a control-pilot signal to the vehicle coupled to the first electric-vehicle connector 126. The control-pilot signal advertises a maximum current, called here the assigned limit, that the vehicle is permitted to draw, and the first control-pilot interface 124 reports whether a vehicle is connected. The first control-pilot interface 124 therefore serves both as the command path by which an assigned limit reaches the vehicle and as a status path by which the presence of a connected vehicle is reported to the edge controller 100.

Each connector station of the plurality further has a local connector identifier. The local connector identifier is a stored value that distinguishes one connector of the plurality from another.

The distinction between the first control-pilot interface 124 and the first connector current sensor 122 is used throughout this description. The first control-pilot interface 124 carries a command. The first connector current sensor 122 carries a measurement. The edge controller 100 does not treat the assigned limit sent through the first control-pilot interface 124 as evidence of the current that the connector is drawing. It obtains that evidence from the first connector current sensor 122.

The edge controller

FIG. 3 shows the edge controller 100 of FIG. 1 in greater detail. The edge controller 100 comprises a nonvolatile memory 132 and is coupled to the isolated local bus 106 and to the network interface 108. The edge controller 100 is further coupled to the branch current sensor 104 of FIG. 1, over which coupling the branch current sensor 104 reports the measured branch current to the edge controller 100. The edge controller 100 carries out the allocation and protection processes described below. The nonvolatile memory 132 retains stored values across loss of supply.

The edge controller 100 is further coupled to a local fault indicator 134 and to a service input 136. The local fault indicator 134 provides an indication at the equipment itself. The service input 136 is an input by which an installer initiates the association of connector identifiers with physical contactors described below.

For each active connector of the plurality, the edge controller 100 stores a charging authorization, a requested current, a minimum sustaining current, and an energy-deficit value. The charging authorization records that the session at that connector is permitted to draw energy. The requested current is a stored value that represents the current requested for the session at that connector. The minimum sustaining current is a stored current value that is assigned to the session at that connector before any further distribution of the available charging current is made. The energy-deficit value is a stored value that represents the energy by which the session at that connector has been under-served.

Allocation at each allocation interval

FIG. 4 shows the process carried out by the edge controller 100. The edge controller 100 repeats the allocation portion of the process at each allocation interval. The allocation interval is a recurring period at which the edge controller 100 revises the assigned limits.

At an available-charging-current determination step 200, the edge controller 100 subtracts a measured non-charging branch load and a configurable safety reserve from the branch limit to obtain an available charging current. The measured non-charging branch load is the measured portion of the branch current that is drawn by the non-charging load 114. The configurable safety reserve is a stored margin that is withheld from allocation, so that the branch is not loaded to its limit.

At a minimum sustaining current assignment step 202, the edge controller 100 first assigns the minimum sustaining current to each session at an active connector that can receive it. The minimum sustaining current stored for each such session is assigned to that session before any further distribution of the available charging current is made.

At a deficit-based distribution step 204, the edge controller 100 distributes the remaining portion of the available charging current, that is, the portion left after the assignments made at the minimum sustaining current assignment step 202, according to energy deficit. Sessions with a greater energy-deficit value receive a greater share of the remaining current than sessions with a smaller energy-deficit value.

At a limit transmission and connector current verification step 206, the edge controller 100 sends each assigned limit through the control-pilot interface of the corresponding connector, and then verifies the resulting connector current with the connector current sensor of that connector. The allocation is in this way closed loop, using per-connector current sensing together with the independent upstream branch sensing described below.

The branch current sensor as an independent safety input

The branch current sensor 104 is an independent safety input. It is not merely a further term in the allocation arithmetic of the available-charging-current determination step 200: it governs a protection process that acts on the power path and that does not depend on the cooperation of any vehicle. The protection process is described with continued reference to FIG. 4.

At a pilot reduction step 208, the edge controller 100 sends a reduced pilot command to at least one connector of the plurality, that is, an assigned limit lower than the limit previously assigned to that connector, and starts a verification interval. The reduced pilot command is the first stage of the staged sequence.

At an ordered contactor shedding step 210, the edge controller 100 determines whether the measured branch current remains above the branch limit after the reduced pilot command and after the verification interval has elapsed. The verification interval is the period that is allowed to elapse after the reduced pilot command before the measured branch current is re-evaluated. If the measured branch current has fallen below the branch limit within the verification interval, the reduced pilot command has been effective and the edge controller 100 returns to the allocation interval process. If the measured branch current remains above the branch limit at the end of the verification interval, the pilot signalling has not produced the required reduction, and the edge controller 100 opens the connector contactors one at a time in a stored shedding order until the measured branch current is below the branch limit.

The shedding order is a stored value that defines the sequence in which the contactors of the plurality of connector stations are opened. The contactors are opened one at a time, with the measured branch current re-evaluated between openings, and as soon as the measured branch current is below the branch limit no further contactor is opened.

The staged sequence of a reduced pilot command, a verification interval, and then ordered interruption of the power path addresses the technical problem set out in the background. A control-pilot command is advisory, and a vehicle that ignores it, responds too slowly, or responds only in coarse steps leaves the branch loaded above its limit. Allocation that works only from commanded values cannot detect that condition at all. By taking the measured branch current from a sensor that is upstream of every connector contactor, and by treating the persistence of an over-limit measurement through a verification interval as the trigger for opening the power path, the edge controller 100 acts at the contactors rather than on the pilot signalling alone.

Recovery after shedding

At a reclose permissive step 212, the edge controller 100 governs the reclosing of a contactor that has been opened by the ordered contactor shedding step 210. The edge controller 100 does not reclose such a contactor until three conditions are satisfied together: the connector current sensor of that connector reports zero current, the control-pilot interface of that connector reports a connected vehicle, and a randomized recovery delay has expired.

On reclosing, the edge controller 100 resumes the allocation interval process, and the reclosed session takes part in the assignments made at the minimum sustaining current assignment step 202 and the deficit-based distribution step 204 in the ordinary way.

Welded-contactor isolation

At a welded-contactor isolation step 214, the edge controller 100 records a welded-contactor condition for a connector when the connector current measured by the connector current sensor of that connector remains above a leakage threshold after an open command has been issued to the contactor of that connector. The leakage threshold is a current magnitude above zero, so that a connector current that has genuinely ceased is distinguished from a connector current that persists after the open command.

When the welded-contactor condition has been recorded, the edge controller 100 inhibits the other contactors on the same branch and operates the local fault indicator 134.

Operation during a network outage

The edge controller 100 can receive remote schedules and authorizations through the network interface 108. The edge controller 100 stores a signed local authorization token and the last accepted branch limit in the nonvolatile memory 132. The signed local authorization token authorizes one or more connectors of the plurality and has an expiry. The last accepted branch limit is the most recent branch limit that the edge controller 100 has accepted.

During a network outage the edge controller 100 continues already authorized sessions subject to the last accepted branch limit and the independent branch-current safety process described above. Charging therefore continues through the outage within the last accepted branch limit. During the outage the last accepted branch limit is the branch limit to which the protection process described above is applied, so that the edge controller 100 sends a reduced pilot command and, when the measured branch current remains above the last accepted branch limit after the reduced pilot command has been sent and the verification interval has elapsed, opens the connector contactors one at a time in the stored shedding order. The edge controller 100 does not start a newly connected session unless the signed local authorization token authorizes that connector and has not expired. No session is started at a connector that the signed local authorization token does not authorize, and no session is started at any connector after the token has expired.

Meter records are appended to a tamper-evident local sequence held locally by the edge controller 100. The records accumulated during the outage are uploaded when communication returns.

Installation mapping through the service input

A multi-connector installation is wired by hand, and the identifier a connector is given in configuration must correspond to the physical contactor and the physical current sensor channel that serve that connector. A transposition assigns a current limit intended for one connector to another. Every component continues to function, every sensor continues to report, and the allocation nevertheless governs the wrong power path.

The service input 136 permits an installer to associate connector identifiers with physical contactors. In response to the service input 136, the edge controller 100 commands one low-energy pilot transition at a time, that is, it drives a pilot transition at a single connector, and confirms the corresponding connector current sensor by observing which sensed channel responds to that transition. The transition is a low-energy one, sufficient to produce a distinguishable response at the connector current sensor of the connector that is actually being driven, and the transitions are commanded one at a time so that the responding channel is unambiguous.

The association between a connector identifier and a physical contactor is stored only after the sensed channel agrees with the commanded connector. If the responding channel is not the channel expected for the commanded connector, or if no channel responds, no association is stored for that connector. The mapping is thus established by measurement on the same sensors that the allocation process later relies upon, so a wiring-map error is found during installation instead of silently misdirecting current limits for the life of the installation.

General considerations

The allocation process, the branch protection process, the offline authorization behaviour, the welded-contactor isolation, the tamper-evident record sequence, and the installation mapping are described together because they share the sensors and the isolated local bus 106 of a single edge controller 100. They are nevertheless separable. An embodiment may carry out the allocation and branch protection processes without the offline authorization behaviour, without the welded-contactor isolation, without the tamper-evident record sequence, or without the installation mapping described above, and an embodiment may include any combination of those additional features.

The branch limit, the configurable safety reserve, the minimum sustaining current, and the shedding order are values stored by the edge controller 100.

The terms first and second are used to distinguish one connector station of the plurality from another and do not imply an order of importance or of operation. The system is not limited to two connector stations, and the description of the first connector station 110 applies to each connector station of the plurality. The embodiments described here are illustrative, and a person skilled in the art will recognize that elements described in connection with one embodiment may be combined with elements described in connection with another. The scope of the invention is defined by the claims that follow.
