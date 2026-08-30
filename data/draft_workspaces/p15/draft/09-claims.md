1. An apparatus for controlling charging of electric vehicles at a plurality of vehicle connectors supplied by a common electrical branch having a branch current limit that is lower than a sum of current ratings of the vehicle connectors, the apparatus comprising:

a plurality of connector channels, each connector channel comprising a contactor connected between the electrical branch and a respective one of the vehicle connectors, a connector current sensor arranged to measure a connector current delivered through the respective one of the vehicle connectors, and a control-pilot interface, each connector channel being associated with a local connector identifier;

a branch current sensor arranged to measure a branch current in the electrical branch upstream of the contactors of the plurality of connector channels; and

a controller coupled to the control-pilot interfaces of the plurality of connector channels, to the connector current sensors of the plurality of connector channels, to the contactors of the plurality of connector channels and to the branch current sensor, the controller being configured to:

store, for each active connector channel of the plurality of connector channels, a charging authorization, a requested current, a minimum sustaining current and an energy-deficit value;

at each allocation interval of a succession of allocation intervals, subtract a measured non-charging branch load and a configurable safety reserve from the branch current limit to obtain an available charging current, assign the minimum sustaining current from the available charging current to each active connector channel that can receive the minimum sustaining current, and distribute a remainder of the available charging current among the active connector channels according to the stored energy-deficit values, thereby obtaining an assigned current limit for each active connector channel;

send the assigned current limit for each active connector channel through the control-pilot interface of that connector channel, and verify the connector current resulting from the assigned current limit with the connector current sensor of that connector channel;

receive a measured branch current from the branch current sensor;

send a reduced pilot command responsive to the measured branch current being above the branch current limit; and

responsive to the measured branch current remaining above the branch current limit for a verification interval after the reduced pilot command has been sent, open the contactors of the plurality of connector channels one at a time in a stored shedding order until the measured branch current is below the branch current limit.

2. The apparatus of claim 1, wherein the controller is further configured to measure the branch current with the branch current sensor after each opening of one of the contactors in the stored shedding order, and to open no further contactor once the measured branch current is below the branch current limit.

3. The apparatus of claim 1, wherein the controller is further configured to withhold reclosure of the contactor of a connector channel that has been opened until the connector current sensor of that connector channel reports zero connector current, the control-pilot interface of that connector channel reports a connected vehicle, and a randomized recovery delay has expired.

4. The apparatus of claim 1, further comprising a local fault indicator, wherein the controller is further configured to record a welded-contactor condition when the connector current of a connector channel remains above a leakage threshold after an open command has been issued to the contactor of that connector channel, and, responsive to the recorded welded-contactor condition, to inhibit the contactors of the other connector channels of the plurality of connector channels and to operate the local fault indicator.

5. The apparatus of claim 1, further comprising an isolated local bus coupling the controller to the control-pilot interfaces of the plurality of connector channels and to the contactors of the plurality of connector channels.

6. A method of controlling charging of electric vehicles at a plurality of vehicle connectors supplied by a common electrical branch having a branch current limit that is lower than a sum of current ratings of the vehicle connectors, the vehicle connectors being served by a plurality of connector channels, each connector channel comprising a contactor connected between the electrical branch and a respective one of the vehicle connectors, a connector current sensor and a control-pilot interface, the method comprising:

storing, for each active connector channel of the plurality of connector channels, a charging authorization, a requested current, a minimum sustaining current and an energy-deficit value;

measuring a branch current in the electrical branch upstream of the contactors of the plurality of connector channels with a branch current sensor to obtain a measured branch current;

at each allocation interval of a succession of allocation intervals, subtract a measured non-charging branch load and a configurable safety reserve from the branch current limit to obtain an available charging current, assign the minimum sustaining current from the available charging current to each active connector channel that can receive the minimum sustaining current, and distributing a remainder of the available charging current among the active connector channels according to the stored energy-deficit values, thereby obtaining an assigned current limit for each active connector channel;

sending the assigned current limit for each active connector channel through the control-pilot interface of that connector channel, and verifying a connector current resulting from the assigned current limit with the connector current sensor of that connector channel;

sending a reduced pilot command responsive to the measured branch current being above the branch current limit; and

responsive to the measured branch current remaining above the branch current limit for a verification interval after the reduced pilot command has been sent, opening the contactors of the plurality of connector channels one at a time in a stored shedding order until the measured branch current is below the branch current limit.

7. The method of claim 6, further comprising measuring the branch current with the branch current sensor after each opening of one of the contactors in the stored shedding order, and opening no further contactor once the measured branch current is below the branch current limit.

8. The method of claim 6, further comprising withholding reclosure of the contactor of a connector channel that has been opened until the connector current sensor of that connector channel reports zero connector current, the control-pilot interface of that connector channel reports a connected vehicle, and a randomized recovery delay has expired.

9. The method of claim 6, further comprising recording a welded-contactor condition when the connector current of a connector channel remains above a leakage threshold after an open command has been issued to the contactor of that connector channel, and, responsive to the recorded welded-contactor condition, inhibiting the contactors of the other connector channels of the plurality of connector channels and operating a local fault indicator.