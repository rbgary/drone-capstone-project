export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[]

export type Database = {
  graphql_public: {
    Tables: {
      [_ in never]: never
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      graphql: {
        Args: {
          extensions?: Json
          operationName?: string
          query?: string
          variables?: Json
        }
        Returns: Json
      }
    }
    Enums: {
      [_ in never]: never
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
  public: {
    Tables: {
      audit_events: {
        Row: {
          action: string
          actor_email: string | null
          actor_id: string
          detail: Json
          drone_hardware_id: string | null
          flight_id: string | null
          id: string
          mission_id: string | null
          occurred_at: string
          result: string
          session_id: string | null
          source: string
        }
        Insert: {
          action: string
          actor_email?: string | null
          actor_id?: string
          detail?: Json
          drone_hardware_id?: string | null
          flight_id?: string | null
          id: string
          mission_id?: string | null
          occurred_at?: string
          result?: string
          session_id?: string | null
          source: string
        }
        Update: {
          action?: string
          actor_email?: string | null
          actor_id?: string
          detail?: Json
          drone_hardware_id?: string | null
          flight_id?: string | null
          id?: string
          mission_id?: string | null
          occurred_at?: string
          result?: string
          session_id?: string | null
          source?: string
        }
        Relationships: [
          {
            foreignKeyName: "audit_events_actor_id_fkey"
            columns: ["actor_id"]
            isOneToOne: false
            referencedRelation: "profiles"
            referencedColumns: ["id"]
          },
        ]
      }
      drones: {
        Row: {
          created_at: string
          hardware_id: string | null
          id: string
          name: string
          notes: string | null
          uri: string
        }
        Insert: {
          created_at?: string
          hardware_id?: string | null
          id?: string
          name: string
          notes?: string | null
          uri?: string
        }
        Update: {
          created_at?: string
          hardware_id?: string | null
          id?: string
          name?: string
          notes?: string | null
          uri?: string
        }
        Relationships: []
      }
      flights: {
        Row: {
          abort_reason: string | null
          ambient_start: number | null
          created_by: string | null
          csv_path: string | null
          drone_id: string | null
          ended_at: string | null
          error: string | null
          ground_z_m: number | null
          id: string
          mode: string | null
          operator_email: string | null
          operator_name: string | null
          outcome: string | null
          program: string | null
          session_id: string | null
          started_at: string
          status: Database["public"]["Enums"]["flight_status"]
          temp_unit: string
        }
        Insert: {
          abort_reason?: string | null
          ambient_start?: number | null
          created_by?: string | null
          csv_path?: string | null
          drone_id?: string | null
          ended_at?: string | null
          error?: string | null
          ground_z_m?: number | null
          id?: string
          mode?: string | null
          operator_email?: string | null
          operator_name?: string | null
          outcome?: string | null
          program?: string | null
          session_id?: string | null
          started_at?: string
          status?: Database["public"]["Enums"]["flight_status"]
          temp_unit?: string
        }
        Update: {
          abort_reason?: string | null
          ambient_start?: number | null
          created_by?: string | null
          csv_path?: string | null
          drone_id?: string | null
          ended_at?: string | null
          error?: string | null
          ground_z_m?: number | null
          id?: string
          mode?: string | null
          operator_email?: string | null
          operator_name?: string | null
          outcome?: string | null
          program?: string | null
          session_id?: string | null
          started_at?: string
          status?: Database["public"]["Enums"]["flight_status"]
          temp_unit?: string
        }
        Relationships: [
          {
            foreignKeyName: "flights_created_by_fkey"
            columns: ["created_by"]
            isOneToOne: false
            referencedRelation: "profiles"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "flights_drone_id_fkey"
            columns: ["drone_id"]
            isOneToOne: false
            referencedRelation: "drones"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "flights_session_id_fkey"
            columns: ["session_id"]
            isOneToOne: false
            referencedRelation: "sessions"
            referencedColumns: ["id"]
          },
        ]
      }
      gallery_albums: {
        Row: {
          category: string
          cover_image_url: string | null
          created_at: string
          date_label: string
          display_order: number
          id: string
          slug: string
          status: string
          summary: string
          title: string
          updated_at: string
        }
        Insert: {
          category?: string
          cover_image_url?: string | null
          created_at?: string
          date_label?: string
          display_order?: number
          id?: string
          slug: string
          status?: string
          summary?: string
          title?: string
          updated_at?: string
        }
        Update: {
          category?: string
          cover_image_url?: string | null
          created_at?: string
          date_label?: string
          display_order?: number
          id?: string
          slug?: string
          status?: string
          summary?: string
          title?: string
          updated_at?: string
        }
        Relationships: []
      }
      gallery_items: {
        Row: {
          album_id: string
          caption: string
          created_at: string
          credit: string
          display_order: number
          height: number | null
          id: string
          kind: string
          url: string
          width: number | null
        }
        Insert: {
          album_id: string
          caption?: string
          created_at?: string
          credit?: string
          display_order?: number
          height?: number | null
          id?: string
          kind: string
          url: string
          width?: number | null
        }
        Update: {
          album_id?: string
          caption?: string
          created_at?: string
          credit?: string
          display_order?: number
          height?: number | null
          id?: string
          kind?: string
          url?: string
          width?: number | null
        }
        Relationships: [
          {
            foreignKeyName: "gallery_items_album_id_fkey"
            columns: ["album_id"]
            isOneToOne: false
            referencedRelation: "gallery_albums"
            referencedColumns: ["id"]
          },
        ]
      }
      missions: {
        Row: {
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          created_by: string | null
          error: string | null
          flight_id: string | null
          id: string
          name: string
          plan: Json
          status: Database["public"]["Enums"]["mission_status"]
          type: string
          updated_at: string
        }
        Insert: {
          claimed_at?: string | null
          claimed_by?: string | null
          created_at?: string
          created_by?: string | null
          error?: string | null
          flight_id?: string | null
          id?: string
          name?: string
          plan: Json
          status?: Database["public"]["Enums"]["mission_status"]
          type: string
          updated_at?: string
        }
        Update: {
          claimed_at?: string | null
          claimed_by?: string | null
          created_at?: string
          created_by?: string | null
          error?: string | null
          flight_id?: string | null
          id?: string
          name?: string
          plan?: Json
          status?: Database["public"]["Enums"]["mission_status"]
          type?: string
          updated_at?: string
        }
        Relationships: [
          {
            foreignKeyName: "missions_created_by_fkey"
            columns: ["created_by"]
            isOneToOne: false
            referencedRelation: "profiles"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "missions_flight_id_fkey"
            columns: ["flight_id"]
            isOneToOne: false
            referencedRelation: "flights"
            referencedColumns: ["id"]
          },
        ]
      }
      notifications: {
        Row: {
          body: string
          created_at: string
          finding_id: string
          flight_id: string | null
          id: string
          read_at: string | null
          session_id: string | null
          severity: string
          title: string
          user_id: string
        }
        Insert: {
          body: string
          created_at?: string
          finding_id: string
          flight_id?: string | null
          id?: string
          read_at?: string | null
          session_id?: string | null
          severity: string
          title: string
          user_id: string
        }
        Update: {
          body?: string
          created_at?: string
          finding_id?: string
          flight_id?: string | null
          id?: string
          read_at?: string | null
          session_id?: string | null
          severity?: string
          title?: string
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "notifications_finding_id_fkey"
            columns: ["finding_id"]
            isOneToOne: false
            referencedRelation: "pipeline_findings"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "notifications_flight_id_fkey"
            columns: ["flight_id"]
            isOneToOne: false
            referencedRelation: "flights"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "notifications_session_id_fkey"
            columns: ["session_id"]
            isOneToOne: false
            referencedRelation: "sessions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "notifications_user_id_fkey"
            columns: ["user_id"]
            isOneToOne: false
            referencedRelation: "profiles"
            referencedColumns: ["id"]
          },
        ]
      }
      pipeline_findings: {
        Row: {
          created_at: string
          delta: number | null
          end_index: number
          evidence_frames: number[]
          expected: number | null
          flight_id: string | null
          id: string
          image_note: string
          image_support: string | null
          observed: number | null
          pipeline_version: string
          point_ids: string[]
          scope: string
          sentence: string
          session_id: string | null
          severity: string
          signal: string
          start_index: number
          t_end_s: number
          t_start_s: number
          title: string
          unit: string
          x_m: number | null
          y_m: number | null
          z: number | null
          z_m: number | null
        }
        Insert: {
          created_at?: string
          delta?: number | null
          end_index: number
          evidence_frames?: number[]
          expected?: number | null
          flight_id?: string | null
          id: string
          image_note?: string
          image_support?: string | null
          observed?: number | null
          pipeline_version: string
          point_ids?: string[]
          scope?: string
          sentence: string
          session_id?: string | null
          severity: string
          signal: string
          start_index: number
          t_end_s: number
          t_start_s: number
          title: string
          unit: string
          x_m?: number | null
          y_m?: number | null
          z?: number | null
          z_m?: number | null
        }
        Update: {
          created_at?: string
          delta?: number | null
          end_index?: number
          evidence_frames?: number[]
          expected?: number | null
          flight_id?: string | null
          id?: string
          image_note?: string
          image_support?: string | null
          observed?: number | null
          pipeline_version?: string
          point_ids?: string[]
          scope?: string
          sentence?: string
          session_id?: string | null
          severity?: string
          signal?: string
          start_index?: number
          t_end_s?: number
          t_start_s?: number
          title?: string
          unit?: string
          x_m?: number | null
          y_m?: number | null
          z?: number | null
          z_m?: number | null
        }
        Relationships: [
          {
            foreignKeyName: "pipeline_findings_flight_id_fkey"
            columns: ["flight_id"]
            isOneToOne: false
            referencedRelation: "flights"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "pipeline_findings_session_id_fkey"
            columns: ["session_id"]
            isOneToOne: false
            referencedRelation: "sessions"
            referencedColumns: ["id"]
          },
        ]
      }
      pipeline_results: {
        Row: {
          created_at: string
          failures: Json
          findings_count: number
          flags: Json
          flight_id: string
          frames: Json
          pipeline_version: string
          points: Json
          segments: Json
          session_id: string | null
          stages: Json
          summary: Json
          temp_unit: string
          tracks: Json
          uploaded_at: string
          uploaded_by: string | null
          worst_severity: string | null
        }
        Insert: {
          created_at: string
          failures?: Json
          findings_count?: number
          flags?: Json
          flight_id: string
          frames?: Json
          pipeline_version: string
          points?: Json
          segments?: Json
          session_id?: string | null
          stages?: Json
          summary?: Json
          temp_unit?: string
          tracks?: Json
          uploaded_at?: string
          uploaded_by?: string | null
          worst_severity?: string | null
        }
        Update: {
          created_at?: string
          failures?: Json
          findings_count?: number
          flags?: Json
          flight_id?: string
          frames?: Json
          pipeline_version?: string
          points?: Json
          segments?: Json
          session_id?: string | null
          stages?: Json
          summary?: Json
          temp_unit?: string
          tracks?: Json
          uploaded_at?: string
          uploaded_by?: string | null
          worst_severity?: string | null
        }
        Relationships: [
          {
            foreignKeyName: "pipeline_results_flight_id_fkey"
            columns: ["flight_id"]
            isOneToOne: true
            referencedRelation: "flights"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "pipeline_results_session_id_fkey"
            columns: ["session_id"]
            isOneToOne: false
            referencedRelation: "sessions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "pipeline_results_uploaded_by_fkey"
            columns: ["uploaded_by"]
            isOneToOne: false
            referencedRelation: "profiles"
            referencedColumns: ["id"]
          },
        ]
      }
      pipeline_session_results: {
        Row: {
          created_at: string
          failures: Json
          findings_count: number
          flags: Json
          frames: Json
          pipeline_version: string
          points: Json
          segments: Json
          session_id: string
          stages: Json
          summary: Json
          temp_unit: string
          tracks: Json
          uploaded_at: string
          uploaded_by: string | null
          worst_severity: string | null
        }
        Insert: {
          created_at: string
          failures?: Json
          findings_count?: number
          flags?: Json
          frames?: Json
          pipeline_version: string
          points?: Json
          segments?: Json
          session_id: string
          stages?: Json
          summary?: Json
          temp_unit?: string
          tracks?: Json
          uploaded_at?: string
          uploaded_by?: string | null
          worst_severity?: string | null
        }
        Update: {
          created_at?: string
          failures?: Json
          findings_count?: number
          flags?: Json
          frames?: Json
          pipeline_version?: string
          points?: Json
          segments?: Json
          session_id?: string
          stages?: Json
          summary?: Json
          temp_unit?: string
          tracks?: Json
          uploaded_at?: string
          uploaded_by?: string | null
          worst_severity?: string | null
        }
        Relationships: [
          {
            foreignKeyName: "pipeline_session_results_session_id_fkey"
            columns: ["session_id"]
            isOneToOne: true
            referencedRelation: "sessions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "pipeline_session_results_uploaded_by_fkey"
            columns: ["uploaded_by"]
            isOneToOne: false
            referencedRelation: "profiles"
            referencedColumns: ["id"]
          },
        ]
      }
      predictions: {
        Row: {
          created_at: string
          disease_risk: number | null
          flight_id: string
          health_score: number | null
          id: string
          label: string | null
          model: string
          notes: string | null
          sample_count: number | null
          zone_id: string | null
        }
        Insert: {
          created_at?: string
          disease_risk?: number | null
          flight_id: string
          health_score?: number | null
          id?: string
          label?: string | null
          model?: string
          notes?: string | null
          sample_count?: number | null
          zone_id?: string | null
        }
        Update: {
          created_at?: string
          disease_risk?: number | null
          flight_id?: string
          health_score?: number | null
          id?: string
          label?: string | null
          model?: string
          notes?: string | null
          sample_count?: number | null
          zone_id?: string | null
        }
        Relationships: [
          {
            foreignKeyName: "predictions_flight_id_fkey"
            columns: ["flight_id"]
            isOneToOne: false
            referencedRelation: "flights"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "predictions_zone_id_fkey"
            columns: ["zone_id"]
            isOneToOne: false
            referencedRelation: "zones"
            referencedColumns: ["id"]
          },
        ]
      }
      profiles: {
        Row: {
          created_at: string
          email: string
          full_name: string | null
          id: string
          role: string
        }
        Insert: {
          created_at?: string
          email: string
          full_name?: string | null
          id: string
          role?: string
        }
        Update: {
          created_at?: string
          email?: string
          full_name?: string | null
          id?: string
          role?: string
        }
        Relationships: []
      }
      project_members: {
        Row: {
          display_order: number
          member_id: string
          project_id: string
        }
        Insert: {
          display_order?: number
          member_id: string
          project_id: string
        }
        Update: {
          display_order?: number
          member_id?: string
          project_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "project_members_member_id_fkey"
            columns: ["member_id"]
            isOneToOne: false
            referencedRelation: "team_members"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "project_members_project_id_fkey"
            columns: ["project_id"]
            isOneToOne: false
            referencedRelation: "projects"
            referencedColumns: ["id"]
          },
        ]
      }
      projects: {
        Row: {
          category: string
          content: Json
          cover_image_url: string | null
          created_at: string
          date_label: string
          display_order: number
          id: string
          location: string
          published_at: string | null
          slug: string
          status: string
          subtitle: string
          summary: string
          title: string
          updated_at: string
        }
        Insert: {
          category?: string
          content?: Json
          cover_image_url?: string | null
          created_at?: string
          date_label?: string
          display_order?: number
          id?: string
          location?: string
          published_at?: string | null
          slug: string
          status?: string
          subtitle?: string
          summary?: string
          title?: string
          updated_at?: string
        }
        Update: {
          category?: string
          content?: Json
          cover_image_url?: string | null
          created_at?: string
          date_label?: string
          display_order?: number
          id?: string
          location?: string
          published_at?: string | null
          slug?: string
          status?: string
          subtitle?: string
          summary?: string
          title?: string
          updated_at?: string
        }
        Relationships: []
      }
      session_samples: {
        Row: {
          battery_v: number | null
          height_m: number | null
          lighthouse_received: number | null
          mode: string | null
          pitch_deg: number | null
          positioned: boolean | null
          raw_temp: number | null
          recorded_at: string
          roll_deg: number | null
          seq: number
          session_id: string
          station_pressure_hpa: number | null
          thrust: number | null
          values: Json
          x_m: number | null
          y_m: number | null
          yaw_deg: number | null
          z_m: number | null
        }
        Insert: {
          battery_v?: number | null
          height_m?: number | null
          lighthouse_received?: number | null
          mode?: string | null
          pitch_deg?: number | null
          positioned?: boolean | null
          raw_temp?: number | null
          recorded_at: string
          roll_deg?: number | null
          seq: number
          session_id: string
          station_pressure_hpa?: number | null
          thrust?: number | null
          values?: Json
          x_m?: number | null
          y_m?: number | null
          yaw_deg?: number | null
          z_m?: number | null
        }
        Update: {
          battery_v?: number | null
          height_m?: number | null
          lighthouse_received?: number | null
          mode?: string | null
          pitch_deg?: number | null
          positioned?: boolean | null
          raw_temp?: number | null
          recorded_at?: string
          roll_deg?: number | null
          seq?: number
          session_id?: string
          station_pressure_hpa?: number | null
          thrust?: number | null
          values?: Json
          x_m?: number | null
          y_m?: number | null
          yaw_deg?: number | null
          z_m?: number | null
        }
        Relationships: [
          {
            foreignKeyName: "session_samples_session_id_fkey"
            columns: ["session_id"]
            isOneToOne: false
            referencedRelation: "sessions"
            referencedColumns: ["id"]
          },
        ]
      }
      sessions: {
        Row: {
          agent_id: string | null
          drone_id: string | null
          end_reason: string | null
          ended_at: string | null
          id: string
          mode_at_start: string | null
          operator_email: string | null
          operator_id: string | null
          operator_name: string | null
          started_at: string
        }
        Insert: {
          agent_id?: string | null
          drone_id?: string | null
          end_reason?: string | null
          ended_at?: string | null
          id: string
          mode_at_start?: string | null
          operator_email?: string | null
          operator_id?: string | null
          operator_name?: string | null
          started_at?: string
        }
        Update: {
          agent_id?: string | null
          drone_id?: string | null
          end_reason?: string | null
          ended_at?: string | null
          id?: string
          mode_at_start?: string | null
          operator_email?: string | null
          operator_id?: string | null
          operator_name?: string | null
          started_at?: string
        }
        Relationships: [
          {
            foreignKeyName: "sessions_drone_id_fkey"
            columns: ["drone_id"]
            isOneToOne: false
            referencedRelation: "drones"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "sessions_operator_id_fkey"
            columns: ["operator_id"]
            isOneToOne: false
            referencedRelation: "profiles"
            referencedColumns: ["id"]
          },
        ]
      }
      site_pages: {
        Row: {
          content: Json
          key: string
          updated_at: string
          updated_by: string | null
        }
        Insert: {
          content?: Json
          key: string
          updated_at?: string
          updated_by?: string | null
        }
        Update: {
          content?: Json
          key?: string
          updated_at?: string
          updated_by?: string | null
        }
        Relationships: []
      }
      team_members: {
        Row: {
          about: string
          avatar_url: string | null
          bio: string
          created_at: string
          current_work: string
          display_order: number
          email: string | null
          full_name: string
          headline: string
          hobbies: Json
          id: string
          links: Json
          location: string
          photos: Json
          role: string
          slug: string | null
          updated_at: string
          website_url: string | null
        }
        Insert: {
          about?: string
          avatar_url?: string | null
          bio?: string
          created_at?: string
          current_work?: string
          display_order?: number
          email?: string | null
          full_name: string
          headline?: string
          hobbies?: Json
          id?: string
          links?: Json
          location?: string
          photos?: Json
          role?: string
          slug?: string | null
          updated_at?: string
          website_url?: string | null
        }
        Update: {
          about?: string
          avatar_url?: string | null
          bio?: string
          created_at?: string
          current_work?: string
          display_order?: number
          email?: string | null
          full_name?: string
          headline?: string
          hobbies?: Json
          id?: string
          links?: Json
          location?: string
          photos?: Json
          role?: string
          slug?: string | null
          updated_at?: string
          website_url?: string | null
        }
        Relationships: []
      }
      telemetry: {
        Row: {
          acc_x_g: number | null
          acc_y_g: number | null
          acc_z_g: number | null
          air_density_kg_m3: number | null
          ambient_est: number | null
          battery_v: number | null
          corrected_temp: number | null
          deviation: number | null
          event: string | null
          expected_raw: number | null
          flight_id: string
          gyro_x_deg_s: number | null
          gyro_y_deg_s: number | null
          gyro_z_deg_s: number | null
          id: number
          index: number
          lighthouse_received: number | null
          mode: string | null
          motor_m1: number | null
          motor_m2: number | null
          motor_m3: number | null
          motor_m4: number | null
          pitch_deg: number | null
          point_id: string | null
          pressure_altitude_m: number | null
          raw_temp: number | null
          recorded_at: string
          roc_per_s: number | null
          roll_deg: number | null
          sea_level_pressure_hpa: number | null
          station_pressure_hpa: number | null
          temp_unit: string
          thermal_offset: number | null
          thermal_state: string | null
          thrust: number | null
          vx_m_s: number | null
          vy_m_s: number | null
          vz_m_s: number | null
          x_m: number | null
          y_m: number | null
          yaw_deg: number | null
          z_m: number | null
        }
        Insert: {
          acc_x_g?: number | null
          acc_y_g?: number | null
          acc_z_g?: number | null
          air_density_kg_m3?: number | null
          ambient_est?: number | null
          battery_v?: number | null
          corrected_temp?: number | null
          deviation?: number | null
          event?: string | null
          expected_raw?: number | null
          flight_id: string
          gyro_x_deg_s?: number | null
          gyro_y_deg_s?: number | null
          gyro_z_deg_s?: number | null
          id?: number
          index: number
          lighthouse_received?: number | null
          mode?: string | null
          motor_m1?: number | null
          motor_m2?: number | null
          motor_m3?: number | null
          motor_m4?: number | null
          pitch_deg?: number | null
          point_id?: string | null
          pressure_altitude_m?: number | null
          raw_temp?: number | null
          recorded_at: string
          roc_per_s?: number | null
          roll_deg?: number | null
          sea_level_pressure_hpa?: number | null
          station_pressure_hpa?: number | null
          temp_unit: string
          thermal_offset?: number | null
          thermal_state?: string | null
          thrust?: number | null
          vx_m_s?: number | null
          vy_m_s?: number | null
          vz_m_s?: number | null
          x_m?: number | null
          y_m?: number | null
          yaw_deg?: number | null
          z_m?: number | null
        }
        Update: {
          acc_x_g?: number | null
          acc_y_g?: number | null
          acc_z_g?: number | null
          air_density_kg_m3?: number | null
          ambient_est?: number | null
          battery_v?: number | null
          corrected_temp?: number | null
          deviation?: number | null
          event?: string | null
          expected_raw?: number | null
          flight_id?: string
          gyro_x_deg_s?: number | null
          gyro_y_deg_s?: number | null
          gyro_z_deg_s?: number | null
          id?: number
          index?: number
          lighthouse_received?: number | null
          mode?: string | null
          motor_m1?: number | null
          motor_m2?: number | null
          motor_m3?: number | null
          motor_m4?: number | null
          pitch_deg?: number | null
          point_id?: string | null
          pressure_altitude_m?: number | null
          raw_temp?: number | null
          recorded_at?: string
          roc_per_s?: number | null
          roll_deg?: number | null
          sea_level_pressure_hpa?: number | null
          station_pressure_hpa?: number | null
          temp_unit?: string
          thermal_offset?: number | null
          thermal_state?: string | null
          thrust?: number | null
          vx_m_s?: number | null
          vy_m_s?: number | null
          vz_m_s?: number | null
          x_m?: number | null
          y_m?: number | null
          yaw_deg?: number | null
          z_m?: number | null
        }
        Relationships: [
          {
            foreignKeyName: "telemetry_flight_id_fkey"
            columns: ["flight_id"]
            isOneToOne: false
            referencedRelation: "flights"
            referencedColumns: ["id"]
          },
        ]
      }
      zones: {
        Row: {
          created_at: string
          id: string
          label: string
          x_max: number
          x_min: number
          y_max: number
          y_min: number
        }
        Insert: {
          created_at?: string
          id?: string
          label: string
          x_max: number
          x_min: number
          y_max: number
          y_min: number
        }
        Update: {
          created_at?: string
          id?: string
          label?: string
          x_max?: number
          x_min?: number
          y_max?: number
          y_min?: number
        }
        Relationships: []
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      claim_mission: {
        Args: { agent_id: string; mission_id: string }
        Returns: {
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          created_by: string | null
          error: string | null
          flight_id: string | null
          id: string
          name: string
          plan: Json
          status: Database["public"]["Enums"]["mission_status"]
          type: string
          updated_at: string
        }
        SetofOptions: {
          from: "*"
          to: "missions"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      claim_next_mission: {
        Args: { agent_id: string }
        Returns: {
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          created_by: string | null
          error: string | null
          flight_id: string | null
          id: string
          name: string
          plan: Json
          status: Database["public"]["Enums"]["mission_status"]
          type: string
          updated_at: string
        }
        SetofOptions: {
          from: "*"
          to: "missions"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      is_operator: { Args: never; Returns: boolean }
      release_stale_claims: { Args: { older_than?: string }; Returns: number }
    }
    Enums: {
      flight_status: "running" | "completed" | "aborted" | "failed"
      mission_status:
        | "queued"
        | "claimed"
        | "running"
        | "done"
        | "failed"
        | "cancelled"
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
}

type DatabaseWithoutInternals = Omit<Database, "__InternalSupabase">

type DefaultSchema = DatabaseWithoutInternals[Extract<keyof Database, "public">]

export type Tables<
  DefaultSchemaTableNameOrOptions extends
    | keyof (DefaultSchema["Tables"] & DefaultSchema["Views"])
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
        DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
      DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])[TableName] extends {
      Row: infer R
    }
    ? R
    : never
  : DefaultSchemaTableNameOrOptions extends keyof (DefaultSchema["Tables"] &
        DefaultSchema["Views"])
    ? (DefaultSchema["Tables"] &
        DefaultSchema["Views"])[DefaultSchemaTableNameOrOptions] extends {
        Row: infer R
      }
      ? R
      : never
    : never

export type TablesInsert<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Insert: infer I
    }
    ? I
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Insert: infer I
      }
      ? I
      : never
    : never

export type TablesUpdate<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Update: infer U
    }
    ? U
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Update: infer U
      }
      ? U
      : never
    : never

export type Enums<
  DefaultSchemaEnumNameOrOptions extends
    | keyof DefaultSchema["Enums"]
    | { schema: keyof DatabaseWithoutInternals },
  EnumName extends DefaultSchemaEnumNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"]
    : never = never,
> = DefaultSchemaEnumNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"][EnumName]
  : DefaultSchemaEnumNameOrOptions extends keyof DefaultSchema["Enums"]
    ? DefaultSchema["Enums"][DefaultSchemaEnumNameOrOptions]
    : never

export type CompositeTypes<
  PublicCompositeTypeNameOrOptions extends
    | keyof DefaultSchema["CompositeTypes"]
    | { schema: keyof DatabaseWithoutInternals },
  CompositeTypeName extends PublicCompositeTypeNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"]
    : never = never,
> = PublicCompositeTypeNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"][CompositeTypeName]
  : PublicCompositeTypeNameOrOptions extends keyof DefaultSchema["CompositeTypes"]
    ? DefaultSchema["CompositeTypes"][PublicCompositeTypeNameOrOptions]
    : never

export const Constants = {
  graphql_public: {
    Enums: {},
  },
  public: {
    Enums: {
      flight_status: ["running", "completed", "aborted", "failed"],
      mission_status: [
        "queued",
        "claimed",
        "running",
        "done",
        "failed",
        "cancelled",
      ],
    },
  },
} as const

