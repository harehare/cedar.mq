// Album sharing policies
@id("view-photos")
permit (
  principal in Group::"viewers",
  action in [Action::"view", Action::"list"],
  resource is Photo in Album::"trip"
)
when { resource.public || principal == resource.owner }
unless { resource has archived && resource.archived };

forbid (principal, action, resource)
unless { context.mfa && context.ip.isIpv4() };
