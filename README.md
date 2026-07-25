# open-carrier-portal
Open carrier portal with API

The purpose of Open Carrier Portal [OCP] is to facilite the sharing of information between Internet Service Providers, Internet Exchanges, Data Centers and Transport Providers.

The implementation will primarily consist of a rest API as well as a reference implementation. The reference implementation will be targeted at smaller carriers and data centers.

Network Information Functionality

For instance, a data center could date one of their customer a particular CFA space for the purpose of issueing LOAs. The carrier, in their own portal, could issue a LOA/CFA and notify both the data center and the other party of the availablity of the allocated port. The data center could optionally issue paperwork for the ordering party to place a cross connect order. Following completion, that port status could be available for either party to query. So during turn up the installing party could verify the light levels at the far end receiver without contacting a NOC. Further, the data center operator could automatically get a notification that the polairty and loss reading are within the expected ranges.

A party can choose what information to share. For instance, an IXP could choose to share port status such as alarms, light levels or traffic stats. A party could also offer the functionality to open tickets for issues.

Social Network Functionality

In the portal, carriers would have the ability to establish a relationship with other entities and then restrict their communications to those entities. The relationships are bilateral only and require mutual consent. Parties outside the system could fall back to industry standard emails. The relationships would be between companies.

Tax/Compliance Functionality

The portal could also be used to exchange (and verify) compliance information such as 499 status in the US.

Desired integrations

IXP Manager
FullCTL
Observium
LibreNMS
PeeringDB
NetBox
Docusign/AdobeSign etc.
