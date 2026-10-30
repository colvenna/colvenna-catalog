package com.colvenna.catalog.fs;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.List;
import org.junit.jupiter.api.Test;

class PathRulesTest {

    @Test
    void rootsNormalizeWithoutAPath() {
        assertThat(PathRules.normalize("s3://bucket/")).isEqualTo("s3://bucket");
        assertThat(PathRules.normalize("s3a://bucket//")).isEqualTo("s3://bucket");
        assertThat(PathRules.normalize("S3://bucket/x/..")).isEqualTo("s3://bucket");
        assertThat(PathRules.normalize("s3://bucket//a//b/")).isEqualTo("s3://bucket/a/b");
        assertThat(PathRules.normalize("file:///")).isEqualTo("file://");
        assertThat(PathRules.normalize("/tmp/x/")).isEqualTo("file:///tmp/x");
        assertThat(PathRules.normalize("file:/tmp/x")).isEqualTo("file:///tmp/x");
    }

    @Test
    void aBucketRootCannotContainTheWarehouse() {
        PathRules rules = new PathRules("s3://bucket/wh", List.of("s3://bucket"));
        for (String location :
                List.of("s3://bucket", "s3://bucket/", "s3a://bucket/", "S3://bucket/x/..")) {
            assertThatThrownBy(() -> rules.checkClientLocation("path", location))
                    .as(location)
                    .hasMessageContaining("overlaps the catalog-managed warehouse");
        }
        assertThatThrownBy(() -> rules.checkClientLocation("path", "s3://bucket/wh/t"))
                .hasMessageContaining("overlaps the catalog-managed warehouse");
        assertThatCode(() -> rules.checkClientLocation("path", "s3://bucket/ext/t"))
                .doesNotThrowAnyException();
        assertThatCode(() -> rules.checkClientLocation("path", "s3://bucket/wh2"))
                .doesNotThrowAnyException();
    }
}
