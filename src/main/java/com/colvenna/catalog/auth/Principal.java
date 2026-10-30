package com.colvenna.catalog.auth;

import java.util.UUID;
import javax.annotation.Nullable;

/** An authenticated service identity and the token it presented. */
public record Principal(String name, boolean admin, @Nullable UUID tokenId) {}
