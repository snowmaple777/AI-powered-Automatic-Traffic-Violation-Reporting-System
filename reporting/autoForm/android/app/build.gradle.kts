plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

val leaflet by configurations.creating
val siteList = tasks.register<Sync>("prepareSiteList") {
    inputs.property("assetName", "sites.txt")
    from(rootProject.file("../檢舉網站.txt"))
    rename("檢舉網站.txt", "sites.txt")
    from(rootProject.file("../../../ui/web")) { into("project") }
    from({ zipTree(leaflet.singleFile) }) {
        val base = "META-INF/resources/webjars/leaflet/1.9.4/"
        include(base + "dist/**", base + "LICENSE")
        eachFile { path = "project/leaflet/" + path.removePrefix(base).removePrefix("dist/") }
        includeEmptyDirs = false
    }
    into(layout.buildDirectory.dir("generated/siteAssets"))
}

android {
    namespace = "tw.nuk.autoform"
    compileSdk = 37
    defaultConfig {
        applicationId = "tw.nuk.autoform"
        minSdk = 26
        targetSdk = 37
        versionCode = 5
        versionName = "0.4.1"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    buildFeatures { compose = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    sourceSets["main"].assets.srcDirs("src/main/assets", rootProject.file("../configs"), layout.buildDirectory.dir("generated/siteAssets").get().asFile)
    sourceSets["test"].resources.srcDir(rootProject.file("../configs"))
}

tasks.named("preBuild").configure { dependsOn(siteList) }

dependencies {
    leaflet("org.webjars.npm:leaflet:1.9.4@jar")
    implementation("androidx.webkit:webkit:1.15.0")
    implementation(platform("androidx.compose:compose-bom:2026.02.01"))
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.documentfile:documentfile:1.0.1")
    implementation("androidx.security:security-crypto:1.1.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:core:1.6.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
}
