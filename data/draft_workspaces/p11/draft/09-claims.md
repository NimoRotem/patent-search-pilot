What is claimed is:

1. A charging control system for a plurality of electric-vehicle connectors supplied by a common electrical branch, the system comprising:

a plurality of connector stations, each connector station of the plurality comprising a contactor connected between the electrical branch and a respective electric-vehicle connector of the plurality of electric-vehicle connectors, a connector current sensor arranged to measure a connector current of the respective electric-vehicle connector, and a control-pilot interface arranged to present an assigned limit to a vehicle coupled to the respective electric-vehicle connector;

a branch current sensor arranged to measure a branch current of the electrical branch upstream of the contactors of the plurality of connector stations;

an isolated local bus; and

a controller coupled to the plurality of connector stations over the isolated local bus and coupled to the branch current sensor, the controller configured to store a branch limit that is lower than a sum of ratings of the plurality of electric-vehicle connectors and to store, for each active connector of the plurality of electric-vehicle connectors, a charging authorization, a requested current, a minimum sustaining current, and an energy-deficit value;

wherein the controller is configured to, at each allocation interval:

subtract, from the branch limit, a measured non-charging branch load of the electrical branch and a configurable safety reserve, to obtain an available charging current;

assign the minimum sustaining current to each session at an active connector that can receive the minimum sustaining current, and then distribute a remaining portion of the available charging current among the active connectors according to the energy-deficit values, thereby obtaining the assigned limit for each of the active connectors;

send each assigned limit through the control-pilot interface of the corresponding connector station; and

verify a resulting connector current of that connector station with the connector current sensor of that connector station; and

wherein the controller is further configured to send a reduced pilot command through the control-pilot interface of at least one connector station of the plurality and to start a verification interval, and, when a measured branch current from the branch current sensor remains above the branch limit after the reduced pilot command has been sent and the verification interval has elapsed, to open the contactors of the plurality of connector stations one at a time in a stored shedding order until the measured branch current is below the branch limit.

2. The system of claim 1, wherein the controller is configured not to reclose a contactor that has been opened in the stored shedding order until the connector current sensor of the connector station of that contactor reports zero connector current, the control-pilot interface of that connector station reports a connected vehicle, and a randomized recovery delay has expired.

3. The system of claim 1, wherein the controller is configured to record a welded-contactor condition for a connector station when the connector current measured by the connector current sensor of that connector station remains above a leakage threshold after an open command has been issued to the contactor of that connector station.

4. The system of claim 3, further comprising a local fault indicator, wherein the controller is configured, upon recording the welded-contactor condition, to inhibit the contactors of the other connector stations supplied by the electrical branch and to operate the local fault indicator.

5. The system of claim 1, further comprising a network interface and a nonvolatile memory, wherein the controller is configured to receive remote schedules and authorizations through the network interface, to store a signed local authorization token and a last accepted branch limit in the nonvolatile memory, and, during a network outage, to continue already authorized sessions subject to the last accepted branch limit, to send the reduced pilot command, and, when the measured branch current from the branch current sensor remains above the last accepted branch limit after the reduced pilot command has been sent and the verification interval has elapsed, to open the contactors of the plurality of connector stations one at a time in the stored shedding order.

6. The system of claim 5, wherein the controller is configured not to start a session at a newly connected electric-vehicle connector during the network outage unless the signed local authorization token authorizes that electric-vehicle connector and has not expired.

7. The system of claim 5, wherein the controller is configured to append meter records to a tamper-evident local sequence and to upload the appended meter records through the network interface when communication returns.

8. The system of claim 1, further comprising a service input, wherein the controller is configured, in response to the service input, to associate local connector identifiers with the contactors of the plurality of connector stations by commanding one low-energy pilot transition at a time through the control-pilot interface of a commanded connector station and confirming a response at the connector current sensor of that connector station, and to store an association between a local connector identifier and a contactor only after the connector current sensor that responds to the commanded low-energy pilot transition agrees with the commanded connector station.

9. The system of claim 1, wherein the controller is configured to re-evaluate the measured branch current between successive openings of the contactors in the stored shedding order and to open no further contactor once the measured branch current is below the branch limit.

10. A method of controlling charging at a plurality of electric-vehicle connectors supplied by a common electrical branch having a branch limit that is lower than a sum of ratings of the plurality of electric-vehicle connectors, each electric-vehicle connector of the plurality being served by a contactor, by a connector current sensor, and by a control-pilot interface, and the electrical branch having a branch current sensor upstream of the contactors, the method comprising:

storing, for each active connector of the plurality of electric-vehicle connectors, a charging authorization, a requested current, a minimum sustaining current, and an energy-deficit value;

at each allocation interval, subtracting from the branch limit a measured non-charging branch load of the electrical branch and a configurable safety reserve, to obtain an available charging current;

assigning the minimum sustaining current to each session at an active connector that can receive the minimum sustaining current, and then distributing a remaining portion of the available charging current among the active connectors according to the energy-deficit values, thereby obtaining an assigned limit for each of the active connectors;

sending each assigned limit through the control-pilot interface of the corresponding electric-vehicle connector and verifying a resulting connector current with the connector current sensor of that electric-vehicle connector;

sending a reduced pilot command through the control-pilot interface of at least one of the electric-vehicle connectors and starting a verification interval; and

when a measured branch current from the branch current sensor remains above the branch limit after the reduced pilot command has been sent and the verification interval has elapsed, opening the contactors one at a time in a stored shedding order until the measured branch current is below the branch limit.

11. The method of claim 10, further comprising withholding reclosure of a contactor opened in the stored shedding order until the connector current sensor of the electric-vehicle connector of that contactor reports zero connector current, the control-pilot interface of that electric-vehicle connector reports a connected vehicle, and a randomized recovery delay has expired.

12. The method of claim 10, further comprising recording a welded-contactor condition for an electric-vehicle connector when the connector current measured by the connector current sensor of that electric-vehicle connector remains above a leakage threshold after an open command has been issued to the contactor of that electric-vehicle connector, and, upon recording the welded-contactor condition, inhibiting the contactors of the other electric-vehicle connectors supplied by the electrical branch and operating a local fault indicator.

13. The method of claim 10, further comprising storing a signed local authorization token and a last accepted branch limit in a nonvolatile memory, and, during a network outage, continuing already authorized sessions subject to the last accepted branch limit and declining to start a session at a newly connected electric-vehicle connector unless the signed local authorization token authorizes that electric-vehicle connector and has not expired.

14. The method of claim 13, further comprising appending meter records to a tamper-evident local sequence during the network outage and uploading the appended meter records when communication returns.

15. The method of claim 10, further comprising associating local connector identifiers with the contactors by commanding one low-energy pilot transition at a time through the control-pilot interface of a commanded electric-vehicle connector, confirming a response at the connector current sensor of that electric-vehicle connector, and storing an association between a local connector identifier and a contactor only after the connector current sensor that responds to the commanded low-energy pilot transition agrees with the commanded electric-vehicle connector.

16. The method of claim 10, further comprising re-evaluating the measured branch current between successive openings of the contactors in the stored shedding order and opening no further contactor once the measured branch current is below the branch limit.
